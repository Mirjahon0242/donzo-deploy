"""
Referral system tests — the actual money movement:

  1. credit_referral_cashback — paid referred order credits referrer
  2. idempotency — same order never credits twice
  3. min order threshold — below 10,000 so'm → no cashback
  4. reverse on refund — reject takes the cashback back
  5. claim writes a ledger entry + moves money
  6. referral_stats returns the fixed home-page link + real earnings
  7. apply-code guards (self, staff, double-link)
"""
from decimal import Decimal

from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APIClient

from apps.users.models import User
from apps.users.referral_service import (
    credit_referral_cashback,
    reverse_referral_cashback,
    REFERRAL_BONUS_PERCENT,
    MIN_ORDER_FOR_REFERRAL,
)
from apps.payments.models import BalanceTransaction
from apps.orders.models import Order, OrderStatus
from apps.services.models import Category, Service, Package


def make_user(username, role='customer', **kw):
    return User.objects.create_user(
        username=username,
        email=f'{username}@test.local',
        password='x',
        role=role,
        **kw,
    )


def _service_package():
    cat, _ = Category.objects.get_or_create(slug='test-cat', defaults={'name': 'Test'})
    svc, _ = Service.objects.get_or_create(
        slug='test-service',
        defaults={'name': 'Test Service', 'category': cat, 'is_active': True},
    )
    pkg, _ = Package.objects.get_or_create(
        service=svc, name='Test Package',
        defaults={'amount_label': 'Test', 'price': Decimal('10000')},
    )
    return svc, pkg


def make_order(customer, total_price, **kw):
    svc, pkg = _service_package()
    return Order.objects.create(
        customer=customer,
        service=svc,
        package=pkg,
        customer_name=customer.username,
        customer_telegram=customer.username,
        total_price=Decimal(str(total_price)),
        status=OrderStatus.PENDING,
        payment_status='paid',
        **kw,
    )


class CreditReferralCashbackTests(TestCase):
    def setUp(self):
        self.referrer = make_user('referrer')
        self.referrer.referral_code = 'REF1'
        self.referrer.save()
        self.customer = make_user('customer')
        self.customer.referred_by = self.referrer
        self.customer.save()

    def test_paid_order_credits_referrer_cashback(self):
        order = make_order(self.customer, 20000)
        tx = credit_referral_cashback(order)

        self.assertIsNotNone(tx)
        self.referrer.refresh_from_db()
        self.assertEqual(
            self.referrer.cashback_balance,
            Decimal('20000') * REFERRAL_BONUS_PERCENT / Decimal('100'),
        )
        # Ledger entry exists
        self.assertTrue(
            BalanceTransaction.objects.filter(
                user=self.referrer, tx_type='cashback', status='completed'
            ).exists()
        )

    def test_idempotent_never_double_credits(self):
        order = make_order(self.customer, 20000)
        credit_referral_cashback(order)
        credit_referral_cashback(order)  # duplicate payment callback
        credit_referral_cashback(order)  # and another retry

        self.referrer.refresh_from_db()
        expected = Decimal('20000') * REFERRAL_BONUS_PERCENT / Decimal('100')
        self.assertEqual(self.referrer.cashback_balance, expected)
        self.assertEqual(
            BalanceTransaction.objects.filter(tx_type='cashback', status='completed').count(),
            1,
        )

    def test_below_min_order_no_cashback(self):
        order = make_order(self.customer, MIN_ORDER_FOR_REFERRAL - Decimal('1'))
        tx = credit_referral_cashback(order)
        self.assertIsNone(tx)
        self.referrer.refresh_from_db()
        self.assertEqual(self.referrer.cashback_balance, Decimal('0'))

    def test_non_referral_order_no_cashback(self):
        stranger = make_user('stranger')
        order = make_order(stranger, 50000)
        self.assertIsNone(credit_referral_cashback(order))

    def test_reverse_on_refund_takes_cashback_back(self):
        order = make_order(self.customer, 20000)
        credit_referral_cashback(order)
        self.referrer.refresh_from_db()
        earned = self.referrer.cashback_balance
        self.assertGreater(earned, 0)

        reverse_referral_cashback(order)
        self.referrer.refresh_from_db()
        self.assertEqual(self.referrer.cashback_balance, Decimal('0'))
        # Ledger tx marked cancelled
        tx = BalanceTransaction.objects.get(
            user=self.referrer, tx_type='cashback', provider_transaction_id=f"REF:{order.order_number}"
        )
        self.assertEqual(tx.status, 'cancelled')

    def test_reverse_claws_back_from_balance_if_already_claimed(self):
        order = make_order(self.customer, 20000)
        credit_referral_cashback(order)
        self.referrer.refresh_from_db()
        earned = self.referrer.cashback_balance

        # Simulate: referrer already claimed the cashback to main balance
        self.referrer.balance = Decimal('50000')
        self.referrer.cashback_balance = Decimal('0')
        self.referrer.save()

        reverse_referral_cashback(order)
        self.referrer.refresh_from_db()
        # 50,000 - 1,000 earned cashback was clawed back
        self.assertEqual(self.referrer.balance, Decimal('50000') - earned)
        self.assertEqual(self.referrer.cashback_balance, Decimal('0'))

    def test_reverse_never_drives_balance_negative(self):
        order = make_order(self.customer, 20000)
        credit_referral_cashback(order)
        self.referrer.refresh_from_db()
        earned = self.referrer.cashback_balance

        self.referrer.balance = Decimal('0')
        self.referrer.cashback_balance = Decimal('0')
        self.referrer.save()

        reverse_referral_cashback(order)
        self.referrer.refresh_from_db()
        self.assertGreaterEqual(self.referrer.balance, 0)
        self.assertGreaterEqual(self.referrer.cashback_balance, 0)


class ReferralViewTests(TestCase):
    def setUp(self):
        self.referrer = make_user('referrer')
        self.referrer.referral_code = 'REF2'
        self.referrer.save()
        self.customer = make_user('customer2')
        self.customer.referral_code = 'CUST2'
        self.customer.referred_by = self.referrer
        self.customer.save()

    def test_stats_link_points_at_home_not_auth_register(self):
        client = APIClient()
        client.force_authenticate(self.referrer)
        res = client.get('/api/v1/auth/referrals/stats/')
        self.assertEqual(res.status_code, 200)
        link = res.data['referral_link']
        self.assertIn('/?ref=REF2', link)
        self.assertNotIn('/auth/register', link)
        self.assertEqual(res.data['bonus_percent'], float(REFERRAL_BONUS_PERCENT))
        self.assertEqual(res.data['min_order_for_referral'], float(MIN_ORDER_FOR_REFERRAL))

    def test_stats_show_real_earnings_from_ledger(self):
        order = make_order(self.customer, 30000)
        credit_referral_cashback(order)
        self.referrer.refresh_from_db()
        client = APIClient()
        client.force_authenticate(self.referrer)
        res = client.get('/api/v1/auth/referrals/stats/')
        expected = float(Decimal('30000') * REFERRAL_BONUS_PERCENT / Decimal('100'))
        self.assertEqual(res.data['total_cashback_earned'], expected)
        self.assertEqual(res.data['available_cashback'], expected)

    def test_my_referrals_shows_real_earned_per_referral(self):
        order = make_order(self.customer, 30000)
        credit_referral_cashback(order)
        client = APIClient()
        client.force_authenticate(self.referrer)
        res = client.get('/api/v1/auth/referrals/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['count'], 1)
        expected = float(Decimal('30000') * REFERRAL_BONUS_PERCENT / Decimal('100'))
        self.assertEqual(res.data['results'][0]['earned_cashback'], expected)
        self.assertEqual(res.data['results'][0]['username'], 'customer2')

    def test_claim_moves_cashback_and_writes_ledger(self):
        order = make_order(self.customer, 50000)
        credit_referral_cashback(order)
        self.referrer.refresh_from_db()
        cashback = self.referrer.cashback_balance
        self.assertGreaterEqual(cashback, Decimal('1000'))

        client = APIClient()
        client.force_authenticate(self.referrer)
        res = client.post('/api/v1/auth/referrals/claim-bonus/')
        self.assertEqual(res.status_code, 200)
        self.referrer.refresh_from_db()
        self.assertEqual(self.referrer.cashback_balance, Decimal('0'))
        self.assertEqual(self.referrer.balance, cashback)
        self.assertTrue(
            BalanceTransaction.objects.filter(
                user=self.referrer, tx_type='cashback_claim', status='completed'
            ).exists()
        )

    def test_apply_code_guards(self):
        guest = make_user('guest')
        guest.referral_code = 'GUEST1'
        guest.save()
        client = APIClient()
        client.force_authenticate(guest)
        # self-referral
        res = client.post('/api/v1/auth/referrals/apply-code/', {'referral_code': 'GUEST1'})
        self.assertEqual(res.status_code, 400)
        # unknown code
        res = client.post('/api/v1/auth/referrals/apply-code/', {'referral_code': 'NOPE'})
        self.assertEqual(res.status_code, 404)
        # staff referrer not allowed
        admin = make_user('admin_user', role='admin')
        admin.referral_code = 'ADMINREF'
        admin.save()
        client2 = APIClient()
        client2.force_authenticate(make_user('guest2'))
        res = client2.post('/api/v1/auth/referrals/apply-code/', {'referral_code': 'ADMINREF'})
        self.assertEqual(res.status_code, 400)

    def test_apply_code_links_user(self):
        stranger = make_user('stranger2')
        client = APIClient()
        client.force_authenticate(stranger)
        res = client.post('/api/v1/auth/referrals/apply-code/', {'referral_code': 'REF2'})
        self.assertEqual(res.status_code, 200)
        stranger.refresh_from_db()
        self.assertEqual(stranger.referred_by, self.referrer)
        # double-link rejected
        res = client.post('/api/v1/auth/referrals/apply-code/', {'referral_code': 'REF2'})
        self.assertEqual(res.status_code, 400)

    def test_admin_stats_show_actual_cashback(self):
        make_order(self.customer, 30000)
        credit_referral_cashback(make_order(self.customer, 30000))
        client = APIClient()
        client.force_authenticate(make_user('super', role='super_admin'))
        res = client.get('/api/v1/admin/referrals/stats/')
        self.assertEqual(res.status_code, 200)
        expected = float(Decimal('30000') * REFERRAL_BONUS_PERCENT / Decimal('100'))
        self.assertEqual(res.data['estimated_cashback_paid'], expected)


class MilestoneRewardTests(TestCase):
    """30 friends → 1 month Telegram Premium gift (45,000 so'm credit)."""

    def setUp(self):
        self.referrer = make_user('milestone_ref')
        self.referrer.referral_code = 'MSREF'
        self.referrer.save()

    def _add_friends(self, n, prefix='friend'):
        from apps.users.referral_service import grant_referral_milestone_rewards
        for i in range(n):
            f = make_user(f'{prefix}_{i}')
            f.referred_by = self.referrer
            f.save()
        return grant_referral_milestone_rewards(self.referrer)

    def test_30_friends_grants_premium_gift(self):
        rewards = self._add_friends(30)
        self.assertEqual(len(rewards), 1)
        self.referrer.refresh_from_db()
        self.assertEqual(self.referrer.balance, Decimal('45000'))
        self.assertTrue(
            BalanceTransaction.objects.filter(
                user=self.referrer, tx_type='referral_gift', status='completed', amount=Decimal('45000')
            ).exists()
        )

    def test_below_30_no_gift(self):
        rewards = self._add_friends(29)
        self.assertEqual(rewards, [])
        self.referrer.refresh_from_db()
        self.assertEqual(self.referrer.balance, Decimal('0'))

    def test_idempotent_never_double_grants(self):
        self._add_friends(30)
        # Running again must NOT grant a second gift for the same milestone
        self.assertEqual(self._add_friends(0), [])
        self.referrer.refresh_from_db()
        self.assertEqual(self.referrer.balance, Decimal('45000'))

    def test_60_friends_grants_second_gift(self):
        self._add_friends(30)
        rewards = self._add_friends(30, prefix='friend_b')  # now 60 total
        self.assertEqual(len(rewards), 1)
        self.referrer.refresh_from_db()
        self.assertEqual(self.referrer.balance, Decimal('90000'))

    def test_apply_code_triggers_milestone_on_30th_friend(self):
        # 29 friends already linked directly
        for i in range(29):
            f = make_user(f'mf_{i}')
            f.referred_by = self.referrer
            f.save()
        self.referrer.refresh_from_db()
        self.assertEqual(self.referrer.balance, Decimal('0'))

        # The 30th friend applies the referral code via the API → gift triggers
        stranger = make_user('mf_30th')
        client = APIClient()
        client.force_authenticate(stranger)
        res = client.post('/api/v1/auth/referrals/apply-code/', {'referral_code': 'MSREF'})
        self.assertEqual(res.status_code, 200)
        self.referrer.refresh_from_db()
        self.assertEqual(self.referrer.balance, Decimal('45000'))
        from apps.users.models import ReferralReward
        self.assertEqual(
            ReferralReward.objects.filter(referrer=self.referrer, status='granted').count(),
            1,
        )

    def test_stats_include_milestone_info(self):
        self._add_friends(12)
        client = APIClient()
        client.force_authenticate(self.referrer)
        res = client.get('/api/v1/auth/referrals/stats/')
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data['milestone_every'], 30)
        self.assertEqual(res.data['milestone_progress'], 12)
        self.assertEqual(res.data['next_milestone'], 30)
        self.assertEqual(res.data['rewards_granted'], 0)
        self.assertEqual(res.data['reward_label'], 'Telegram Premium 1 oy')

    def test_stats_backfill_grants_to_existing_users(self):
        """A user who already has 30 friends gets the gift on next stats view."""
        self._add_friends(30)
        self.referrer.refresh_from_db()
        self.assertEqual(self.referrer.balance, Decimal('45000'))
