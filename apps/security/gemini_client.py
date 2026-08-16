# -*- coding: utf-8 -*-
"""
DONZO Gemini client — umumiy, rotatsiyali.

Muammo: Gemini bepul tier'da HAR MODEL uchun ~20 so'rov/daqiqa limiti bor.
Staff chat, ovoz transkripsiyasi, risk engine va AI copilot hammasi bitta
sozlangan modelga urilganda — 429 (quota) tez tugaydi va "HTTPError" xatosi
chiqadi.

Yechim: sozlangan model birinchi, qolganlari zaxira. Model 429 qaytarsa —
uni qisqa muddatga (cooldown) "charchagan" deb belgilab, keyingi modelga
o'tamiz. Har modelning o'z limiti bor, shuning uchun rotatsiya umumiy
sig'imni bir necha barobar oshiradi.
"""
import json
import logging
import time
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

GEMINI_URL = 'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent'

# Zaxira modellar — sozlangan modeldan keyin sinanadi (audio + matn uchun ishlaydi).
MODEL_POOL = (
    'gemini-3.6-flash',
    'gemini-3.5-flash',
    'gemini-3.7-flash',
    'gemini-3.5-flash-lite',
    'gemini-flash-lite-latest',
    'gemini-3-flash-preview',
)

# Model 429 (quota) qaytarsa shuncha sekundga "charchagan" deb belgilanadi.
_QUOTA_COOLDOWN_SECONDS = 55
# model -> qachongacha kutish kerak (epoch sekund)
_quota_cooldown_until: dict = {}
_lock_used = False  # (oddiy dict — GIL yetarli)


def _mark_quota(model: str, seconds: float = None) -> None:
    cooldown = seconds or _QUOTA_COOLDOWN_SECONDS
    _quota_cooldown_until[model] = time.time() + cooldown
    logger.warning('Gemini %s quota tugadi — %ss cooldown', model, int(cooldown))


def _in_quota_cooldown(model: str) -> bool:
    until = _quota_cooldown_until.get(model, 0)
    if until and time.time() < until:
        return True
    if until:
        _quota_cooldown_until.pop(model, None)
    return False


def _model_order(configured: str):
    """Sozlangan model birinchi, keyin zaxira; cooldown'dagilarni oxirga tashlaydi."""
    order = []
    seen = set()
    for m in ([configured] if configured else []) + list(MODEL_POOL):
        m = (m or '').strip()
        if m and m not in seen:
            seen.add(m)
            order.append(m)
    # cooldown'dagilarni oxiriga tashlaymiz — lekin umuman tashlamaymiz
    # (barchasi charchagan bo'lsa ham urinamiz).
    fresh = [m for m in order if not _in_quota_cooldown(m)]
    tired = [m for m in order if _in_quota_cooldown(m)]
    return fresh + tired


def _parse_retry_seconds(body: str) -> float:
    """429 javobidagi 'retry in Ns' dan kutish vaqtini chiqaradi."""
    try:
        data = json.loads(body)
        msg = data.get('error', {}).get('message', '')
        import re
        m = re.search(r'retry in\s+([\d.]+)\s*s', msg, re.IGNORECASE)
        if m:
            return min(float(m.group(1)), 60.0)
    except Exception:
        pass
    return _QUOTA_COOLDOWN_SECONDS


def _post(url: str, body: dict, timeout: int = 45) -> tuple:
    """POST qiladi. Returns (status, raw_text). HTTPError ham (code, body) qaytadi."""
    req = urllib.request.Request(
        url, data=json.dumps(body).encode('utf-8'),
        headers={'Content-Type': 'application/json'}, method='POST',
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode('utf-8')
    except urllib.error.HTTPError as exc:
        try:
            err_body = exc.read().decode('utf-8')
        except Exception:
            err_body = ''
        return exc.code, err_body
    except Exception as exc:
        raise


def chat(prompt: str, configured_model: str = None, temperature: float = 0.4,
         max_tokens: int = 1024, timeout: int = 45, api_key: str = None) -> dict:
    """Matn so'rovi — rotatsiya bilan. Returns {'ok', 'answer', 'model'}.

    Hech qachon exception tashlamaydi. Barcha modellar xato bersa
    {'ok': False, 'answer': ...} qaytadi.
    """
    if not prompt or not prompt.strip():
        return {'ok': False, 'answer': 'Bo\'sh so\'rov'}
    # API kalit kiritilmasa — sozlamadan o'qiymiz (sinxron kontekst uchun).
    if not api_key:
        try:
            from apps.settings_app.models import Setting
            api_key = Setting.get_setting('gemini_api_key', '') or ''
        except Exception:
            api_key = ''
    if not api_key:
        return {'ok': False, 'answer': 'Gemini API kaliti sozlanmagan (gemini_api_key)'}
    last_code = None
    last_name = None
    for model in _model_order(configured_model or 'gemini-3.6-flash'):
        body = {
            'contents': [{'parts': [{'text': prompt}]}],
            'generationConfig': {'temperature': temperature, 'maxOutputTokens': max_tokens},
        }
        url = GEMINI_URL.format(model=model) + f'?key={api_key}'
        try:
            status, raw = _post(url, body, timeout=timeout)
        except Exception as exc:
            last_name = type(exc).__name__
            logger.warning('Gemini %s call failed: %s', model, last_name)
            continue
        if status == 200:
            try:
                data = json.loads(raw)
                text = (data['candidates'][0]['content']['parts'][0]['text'] or '').strip()
                if text:
                    return {'ok': True, 'answer': text, 'model': model}
            except (KeyError, IndexError, json.JSONDecodeError) as exc:
                logger.warning('Gemini %s unparseable response: %s', model, type(exc).__name__)
                continue
        elif status == 429:
            last_code = 429
            retry = _parse_retry_seconds(raw)
            _mark_quota(model, retry)
            logger.warning('Gemini %s quota (429) — keyingi modelga o\'tish', model)
            continue
        elif status >= 500:
            last_code = status
            logger.warning('Gemini %s server xatosi (%s) — keyingi modelga o\'tish', model, status)
            continue
        else:
            last_code = status
            logger.warning('Gemini %s xato (%s) — keyingi modelga o\'tish', model, status)
            continue
    # Hammasi yiqildi — aniq sabab bilan qaytamiz.
    if last_code == 429:
        return {'ok': False, 'answer': 'AI vaqtincha band (quota limiti). Bir daqiqadan so\'ng qayta urinib ko\'ring.',
                'error': 'quota_exceeded'}
    return {'ok': False, 'answer': f'AI hozircha javob bera olmadi ({"quota" if last_code == 429 else last_name or last_code or "xato"}). Bir ozdan so\'ng qayta urinib ko\'ring.',
            'error': last_name or 'unknown'}


def health_check() -> dict:
    """Tez sog'liq tekshiruvi (admin panel uchun)."""
    res = chat('Salom, bitta so\'z bilan javob ber: ok', max_tokens=10, temperature=0.0)
    return {
        'ok': res['ok'],
        'model': res.get('model'),
        'error': res.get('error'),
        'detail': 'OK' if res['ok'] else res.get('answer', 'xato'),
    }
