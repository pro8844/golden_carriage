"""황금마차 '이번 달 추천' 만들기 (GitHub Actions에서 실행)

1) 암호화된 상품 목록(data/p.bin)을 열어요
2) 네이버 쇼핑 검색 API로 상품마다 온라인 최저가를 찾아 '1개 값'으로 맞춰 비교해요
3) 네이버 데이터랩 쇼핑인사이트(쇼핑 클릭량)로 많이 찾는 상품을 골라요
4) 많이 찾으면서 군마트가 확실히 싼 상품을 골라 data/rec.bin 으로 암호화해 저장해요

필요한 환경변수(GitHub Secrets): HM_PW, NAVER_CLIENT_ID, NAVER_CLIENT_SECRET
키가 없으면 아무것도 바꾸지 않고 끝나요(기존 추천 유지).
"""
import os, re, sys, json, time, math, base64, html, datetime, unicodedata

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from update import make_key, dec, enc, DATA, log  # noqa: E402

SHOP_URL = 'https://openapi.naver.com/v1/search/shop.json'
LAB_URL = 'https://openapi.naver.com/v1/datalab/shopping/category/keywords'
SKIP_CATS = {'의류·피복·신발', '군용품', '기타', '주류', '담배'}   # 온라인 비교가 어렵거나 의미 없는 분류
MIN_SAVE = 0.10        # 군마트가 10% 이상 싸야 추천
TOP_N = 20             # 상단에 보여줄 추천 수
PER_CAT = 2            # 한 분류에서 최대 몇 개
LAB_PER_CAT = 12       # 분류별 인기도(데이터랩) 확인 후보 수
NAVER_CAT = {          # 네이버 쇼핑 대분류 → 데이터랩 카테고리 코드
    '패션의류': '50000000', '패션잡화': '50000001', '화장품/미용': '50000002', '디지털/가전': '50000003',
    '가구/인테리어': '50000004', '출산/육아': '50000005', '식품': '50000006', '스포츠/레저': '50000007',
    '생활/건강': '50000008', '여가/생활편의': '50000009',
}

TAG = re.compile(r'<[^>]+>')
UNIT_RE = r'(?:ml|mL|ML|l|L|g|G|kg|Kg|KG|매|정|포|구|롤|m)'


def norm(t):
    t = unicodedata.normalize('NFKC', html.unescape(TAG.sub('', t or ''))).lower()
    return t


def compact(t):
    return re.sub(r'[^0-9a-z가-힣.]', '', norm(t))


def clean_name(n):
    n = re.sub(r'[\[(](영외|동계|공군|해군|신규)[\])]', ' ', n)
    n = re.sub(r'규격\(\d+\)', ' ', n)
    return re.sub(r'\s+', ' ', n).strip()


def size_token(spec):
    """'120G' → '120g', '1.5Kg' → '1.5kg', '500ml' → '500ml' (단순 용량만, 없으면 None)"""
    m = re.search(r'(\d+(?:\.\d+)?)\s*(ml|l|g|kg)\b', norm(spec))
    if not m:
        return None
    num = m.group(1).rstrip('0').rstrip('.') if '.' in m.group(1) else m.group(1)
    return num + m.group(2)


def pack_count(text):
    """제목·규격에서 묶음 수를 찾아요. '5개입 x 4팩' → 20, '145g*5' → 5, 없으면 1"""
    t = norm(text)
    m = re.search(r'(\d+)\s*(?:개입|입|개|봉|팩|병|캔|ea)\s*[x×*]\s*(\d+)', t)
    if m:
        return int(m.group(1)) * int(m.group(2))
    nums = [int(x) for x in re.findall(r'(\d+)\s*(?:개입|입|개|봉지|봉|팩|병|캔|ea|세트|박스)(?![a-z가-힣])', t)]
    nums += [int(x) for x in re.findall(r'(?:[x×*]\s*)(\d+)(?!\s*' + UNIT_RE + r')', t)]
    nums = [n for n in nums if 1 < n <= 200]
    return max(nums) if nums else 1


def name_tokens(n):
    toks = re.findall(r'[0-9a-z가-힣]+', norm(clean_name(n)))
    return [t for t in toks if len(t) >= 2 and not re.fullmatch(r'\d+(?:ml|l|g|kg|입|개|개입)?', t)]


def match_score(item, title):
    toks = name_tokens(item['n'])
    if not toks:
        return 0.0
    tc = compact(title)
    return sum(1 for t in toks if t in tc) / len(toks)


class Naver:
    def __init__(self, cid, secret, session=None):
        self.s = session or requests.Session()
        self.h = {'X-Naver-Client-Id': cid, 'X-Naver-Client-Secret': secret}
        self.calls = 0

    def _req(self, method, url, **kw):
        for attempt in range(4):
            self.calls += 1
            r = self.s.request(method, url, headers=self.h, timeout=20, **kw)
            if r.status_code == 429:
                time.sleep(1.5 * (attempt + 1)); continue
            if r.status_code in (401, 403):
                raise RuntimeError('네이버 API 키가 맞지 않거나 사용 권한(검색/데이터랩)이 없어요: HTTP %d' % r.status_code)
            r.raise_for_status()
            return r.json()
        raise RuntimeError('네이버 API 호출 한도 초과')

    def shop(self, query):
        time.sleep(0.12)
        return self._req('GET', SHOP_URL, params={'query': query, 'display': 40, 'sort': 'sim',
                                                 'exclude': 'used:rental:cbshop'})

    def lab(self, cat, keywords, start, end):
        body = {'startDate': start, 'endDate': end, 'timeUnit': 'month', 'category': cat,
                'keyword': [{'name': k, 'param': [k]} for k in keywords]}
        time.sleep(0.12)
        return self._req('POST', LAB_URL, json=body)


def compare(item, res):
    """검색 결과에서 같은 상품(이름·용량 일치)만 골라 1개 값 최저가를 찾아요"""
    px_cnt = pack_count(item['s'] + ' ' + item['n'])
    px_unit = item['p'] / px_cnt
    size = size_token(item['s']) or size_token(item['n'])
    best = None
    for r in res.get('items', []):
        title = norm(r.get('title', ''))
        try:
            lp = int(r.get('lprice') or 0)
        except ValueError:
            continue
        if lp <= 0 or match_score(item, title) < 0.7:
            continue
        if size and size not in compact(title):
            continue
        cnt = pack_count(title)
        unit = lp / cnt
        ratio = unit / px_unit
        if not (0.35 <= ratio <= 4):          # 크기·구성이 다른 상품일 가능성이 커서 제외
            continue
        if best is None or unit < best['unit']:
            best = {'unit': unit, 'lp': lp, 'cnt': cnt, 'mall': r.get('mallName') or '', 'link': r.get('link') or '',
                    'cat1': r.get('category1') or ''}
    if not best:
        return None
    best['px_unit'] = px_unit
    best['px_cnt'] = px_cnt
    best['save'] = 1 - px_unit / best['unit']
    return best


def main(api=None, now=None):
    pw = os.environ.get('HM_PW')
    cid, sec = os.environ.get('NAVER_CLIENT_ID'), os.environ.get('NAVER_CLIENT_SECRET')
    if not api and not (cid and sec):
        log('NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 이 없어서 추천을 건너뛰어요 (기존 추천 유지)')
        return 0
    api = api or Naver(cid, sec)
    meta = json.load(open(os.path.join(DATA, 'meta.json')))
    aes = make_key(pw, base64.b64decode(meta['salt']), meta['iter'])
    prod = dec(aes, os.path.join(DATA, 'p.bin'))
    items = [i for i in prod['items'] if i['c'] not in SKIP_CATS and not i['n'].startswith('규격(')]
    log('비교 대상', len(items), '개')

    found = {}
    for k, it in enumerate(items):
        q = clean_name(it.get('q') or it['n'])
        size = size_token(it['s'])
        if size and size not in compact(q):
            q += ' ' + size
        try:
            res = api.shop(q)
        except RuntimeError:
            raise
        except Exception as e:
            log('  검색 실패', it['n'], e); continue
        c = compare(it, res)
        if c:
            c['total'] = int(res.get('total') or 0)
            c['kw'] = clean_name(it.get('q') or it['n'])
            found[it['id']] = c
        if k % 200 == 0:
            log('  진행 %d/%d (찾음 %d)' % (k, len(items), len(found)))
    log('온라인 최저가 찾음', len(found), '개 / 호출', api.calls)

    # ---- 인기도: 분류마다 싼 후보를 쇼핑 클릭량(데이터랩)으로 비교 ----
    now = now or (datetime.datetime.utcnow() + datetime.timedelta(hours=9))
    end = (now.replace(day=1) - datetime.timedelta(days=1))
    start = (end.replace(day=1) - datetime.timedelta(days=62)).replace(day=1)
    by_id = {i['id']: i for i in items}
    cands = [i for i in found if found[i]['save'] >= MIN_SAVE]
    for iid in cands:
        found[iid]['pop'] = math.log10(found[iid]['total'] + 10) / 10    # 기본값: 판매처 수(약한 신호)
    groups = {}
    for iid in cands:
        groups.setdefault(by_id[iid]['c'], []).append(iid)
    lab_ok = 0
    for cat, ids in groups.items():
        ids.sort(key=lambda i: -found[i]['total'])
        ids = ids[:LAB_PER_CAT]
        ncat = NAVER_CAT.get(found[ids[0]]['cat1'])
        if not ncat:
            continue
        anchor = ids[0]
        for b in range(1, len(ids), 4):
            batch = [anchor] + ids[b:b + 4]
            kws = list(dict.fromkeys(found[i]['kw'][:30] for i in batch))
            try:
                data = api.lab(ncat, kws, start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d'))
            except RuntimeError as e:
                log('  데이터랩 사용 불가:', e); groups = {}; break
            except Exception as e:
                log('  데이터랩 실패', cat, e); continue
            avg = {}
            for r in data.get('results', []):
                vals = [d.get('ratio', 0) for d in r.get('data', [])]
                avg[r.get('title')] = sum(vals) / len(vals) if vals else 0
            base = avg.get(found[anchor]['kw'][:30]) or 0
            if base <= 0:
                continue
            for i in batch:
                v = avg.get(found[i]['kw'][:30])
                if v is not None:
                    found[i]['pop'] = 1 + v / base       # 데이터랩 값이 있으면 판매처 수보다 우선
                    lab_ok += 1
        if not groups:
            break
    log('쇼핑 클릭량 확인', lab_ok, '건')

    # ---- 고르기: 많이 찾는(인기 상위 절반) 상품 중 절약률 높은 순, 분류당 최대 PER_CAT ----
    ranked = sorted(cands, key=lambda i: -found[i]['pop'])
    pool = ranked[:max(TOP_N * 2, len(ranked) // 2)]
    pool.sort(key=lambda i: -found[i]['save'])
    top, per = [], {}
    for iid in pool:
        c = by_id[iid]['c']
        if per.get(c, 0) >= PER_CAT:
            continue
        per[c] = per.get(c, 0) + 1
        top.append(iid)
        if len(top) >= TOP_N:
            break

    today = now.strftime('%Y-%m-%d')
    out = {
        'updated': today, 'basis': '네이버쇼핑',
        'top': top,
        # 비교된 모든 상품: [온라인 1개 값, 온라인 묶음 수, 판매처, 링크, 절약률(%), 군마트 묶음 수]
        'cmp': {i: [round(f['unit']), f['cnt'], f['mall'], f['link'], round(f['save'] * 100), f['px_cnt']] for i, f in found.items()},
    }
    enc(aes, out, os.path.join(DATA, 'rec.bin'))
    meta['ver'] = int(time.time())
    json.dump(meta, open(os.path.join(DATA, 'meta.json'), 'w'))
    lines = ['- %s · 군마트 %s원 / 온라인 1개 %s원 (%d%% 저렴, %s)' % (
        by_id[i]['n'], format(round(found[i]['px_unit']), ','), format(round(found[i]['unit']), ','),
        round(found[i]['save'] * 100), found[i]['mall']) for i in top]
    summary = '추천 %d개 · 비교 성공 %d개 · API 호출 %d회 · 기준일 %s' % (len(top), len(found), api.calls, today)
    log(summary)
    for l in lines:
        log(' ', l)
    gs = os.environ.get('GITHUB_STEP_SUMMARY')
    if gs:
        with open(gs, 'a') as f:
            f.write('### 황금마차 이번 달 추천\n\n' + summary + '\n\n' + '\n'.join(lines) + '\n')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:
        log('추천 실패:', e)
        sys.exit(1)
