# -*- coding: utf-8 -*-
"""
IP-geolokatsiya (anti-fraud metadata).

Foydalanuvchining IP manzilidan joylashuvni aniqlaydi (shahar, davlat, ISP).
Bepul ip-api.com (HTTP, kalit talab qilmaydi, 45 so'rov/daqiqa) ishlatiladi.

XAVFSIZLIK / CHEKLOVLAR:
  • Natija 24 soat davomida Django cache'da saqlanadi — bir xil IP uchun
    takror so'rov yuborilmaydi (API limiti va sekinlikning oldini olish);
  • Lokal/private IP'lar (127.x, 10.x, 192.168.x, 172.16-31.x, ::1) uchun
    so'rov yuborilmaydi — ularga joylashuv aniqlab bo'lmaydi;
  • Xato bo'lsa hech qachon login/so'rov buzilmaydi — None qaytadi;
  • Hech qanday maxfiy ma'lumot tashqariga yuborilmaydi — faqat IP.
"""
import ipaddress
import logging

from django.core.cache import cache

logger = logging.getLogger(__name__)

GEO_CACHE_TTL = 24 * 60 * 60  # 24 soat
_TIMEOUT = 3


def _is_private(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip.split('%')[0])
        return addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved
    except ValueError:
        return True  # noto'g'ri IP — so'rov yubormaymiz


def geolocate(ip: str):
    """IP bo'yicha joylashuvni qaytaradi (best-effort, kesh bilan).

    Natija: {'city': 'Toshkent', 'country': 'UZ', 'region': 'Toshkent', 'isp': '...'}
    yoki None (xato / private IP). Hech qachon exception tashlamaydi.
    """
    ip = (ip or '').strip()
    if not ip or _is_private(ip):
        return None

    cache_key = f'geoip:{ip}'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached if cached else None

    try:
        import requests
        resp = requests.get(
            f'http://ip-api.com/json/{ip}',
            params={'fields': 'status,country,countryCode,city,regionName,isp,lat,lon,query'},
            timeout=_TIMEOUT,
        )
        data = resp.json()
        if data.get('status') != 'success':
            cache.set(cache_key, '', GEO_CACHE_TTL)  # manfiy kesh — takror urinmaymiz
            return None
        result = {
            'city': data.get('city') or '',
            'country': data.get('countryCode') or data.get('country') or '',
            'region': data.get('regionName') or '',
            'isp': data.get('isp') or '',
            # Taxminiy koordinata (IP orqali) — xarita uchun. ip-api.com
            # lat/lon qaytaradi; faqat IP bo'yicha TAXMINIY joylashuv.
            'lat': data.get('lat'),
            'lon': data.get('lon'),
        }
        cache.set(cache_key, result, GEO_CACHE_TTL)
        return result
    except Exception:
        logger.debug('Geolokatsiya xatosi (ahamiyatsiz) ip=%s', ip)
        return None


def location_label(geo: dict) -> str:
    """Joylashuvni bitta satrga yig'adi: 'Toshkent, UZ' (mavjud qismlari bilan)."""
    if not geo:
        return ''
    parts = [p for p in (geo.get('city'), geo.get('country')) if p]
    label = ', '.join(parts)
    if geo.get('isp'):
        label = f'{label} · {geo["isp"]}' if label else geo['isp']
    return label[:200]


def reverse_geocode(lat, lng) -> str:
    """GPS koordinatadan TO'LIQ manzilni aniqlaydi (Nominatim/OpenStreetMap).

    Natija: 'Chilonzor tumani, Toshkent shahri, Toshkent viloyati, O'zbekiston'
    yoki '' (xato). Best-effort: 24h kesh, hech qachon exception tashlamaydi.
    Nominatim bepul (1 so'rov/sekund) — shuning uchun kesh muhim.
    """
    try:
        lat_f = float(lat)
        lng_f = float(lng)
    except (TypeError, ValueError):
        return ''
    # Noto'g'ri diapazon — so'rov yubormaymiz
    if not (-90 <= lat_f <= 90) or not (-180 <= lng_f <= 180):
        return ''

    cache_key = f'georev:{lat_f:.4f},{lng_f:.4f}'
    cached = cache.get(cache_key)
    if cached is not None:
        return cached if cached else ''

    try:
        import requests
        resp = requests.get(
            'https://nominatim.openstreetmap.org/reverse',
            params={
                'lat': f'{lat_f:.6f}', 'lon': f'{lng_f:.6f}',
                'format': 'jsonv2', 'addressdetails': 1, 'zoom': 16,
                'accept-language': 'uz',
            },
            headers={'User-Agent': 'DONZO-anti-fraud/1.0'},
            timeout=_TIMEOUT + 2,
        )
        data = resp.json()
        addr = data.get('address') or {}
        if not addr:
            cache.set(cache_key, '', GEO_CACHE_TTL)
            return ''
        # Eng kichikdan kattagacha: ko'cha, tuman, shahar, viloyat, davlat.
        # country_code (masalan 'uz') qo'shilmaydi — to'liq davlat nomi yetarli.
        parts = []
        for key in ('road', 'pedestrian', 'footway', 'neighbourhood',
                    'suburb', 'quarter', 'borough', 'city_district',
                    'town', 'city', 'municipality', 'county',
                    'state_district', 'state', 'region',
                    'country'):
            val = addr.get(key)
            if val and str(val).strip() and str(val).strip() not in parts:
                parts.append(str(val).strip())
        full = ', '.join(p for p in parts if p)
        cache.set(cache_key, full, GEO_CACHE_TTL)
        return full[:300]
    except Exception:
        logger.debug('Reverse geocode xatosi (ahamiyatsiz) lat=%s lng=%s', lat, lng)
        return ''
