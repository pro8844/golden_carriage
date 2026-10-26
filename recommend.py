"""황금마차 '이번 달 추천' 만들기

인기도(자동, GitHub Actions에서 매월):
  네이버 클라우드 NAVER API HUB의 쇼핑인사이트(쇼핑 검색 클릭량)로 군마트 상품 중 많이 찾는 상품을 골라요.
  필요한 Secrets: HM_PW, NAVER_CLIENT_ID, NAVER_CLIENT_SECRET (API HUB Application의 인증 정보)
  키가 없으면 인기 목록은 그대로 두고 끝나요.

온라인 가격(수동):
  네이버 쇼핑 검색 API가 2026-07-31 종료되어 자동 수집 방법이 없어요.
  직접 조사한 가격 파일을 넣을 때만:  python tools/recommend.py --prices manual_prices.json
  (기존 rec.bin 의 가격 정보는 매달 그대로 유지돼요)

결과: data/rec.bin (암호화)
  {updated, basis, top:[id...], pop_date, price:{id:[온라인가(군마트 용량 환산), 설명, 링크]}, price_date, price_mall, price_note}
"""
import os, re, sys, json, time, base64, html, datetime, unicodedata

import requests

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from update import make_key, dec, enc, DATA, log  # noqa: E402

LAB_URL = 'https://naverapihub.apigw.ntruss.com/shopping/v1/category/keywords'
SKIP_CATS = {'의류·피복·신발', '군용품', '기타', '주류', '담배'}
TOP_N = 24
PER_CAT = 3
NAVER_CAT = {   # 우리 분류 → 네이버쇼핑 대분류(cat_id)
    '라면·면류': '50000006', '과자·간식': '50000006', '빵·디저트': '50000006', '아이스크림': '50000006',
    '음료·커피·차': '50000006', '우유·유제품': '50000006', '냉동·간편식': '50000006', '육가공·통조림·반찬': '50000006',
    '쌀·잡곡·즉석밥': '50000006', '양념·소스·식용유': '50000006', '건강식품': '50000006',
    '화장품·뷰티': '50000002', '헤어·바디': '50000002',
    '구강·위생용품': '50000008', '세제·청소·주방': '50000008', '전자·생활잡화': '50000008', '문구': '50000008',
    '유아·반려동물': '50000008', '스포츠·레저': '50000007',
}
ANCHOR = {'50000006': '초코파이', '50000002': '니베아', '50000008': '리스테린', '50000007': '요가매트'}
DROP_WORDS = re.compile(r'기획세트|더블기획|기획|세트|리필|본품|대용량|증정|신규|영외|동계|\d+\+\d+|\d+(?:\.\d+)?\s*(?:ml|l|g|kg|입|개입|개|매|포|ea|p)\b', re.I)


def short_kw(name):
    """쇼핑 검색어처럼 짧게: '[영외]스위스볼트 멀티후레쉬 300g 기획' → '스위스볼트 멀티후레쉬'"""
    n = unicodedata.normalize('NFKC', html.unescape(name))
    n = re.sub(r'[\[(][^\])]*[\])]', ' ', n)
    n = DROP_WORDS.sub(' ', n)
    toks = [t for t in re.findall(r'[0-9A-Za-z가-힣&]+', n) if len(t) >= 2]
    kw = ' '.join(toks[:2])
    return kw[:20]


class Hub:
    def __init__(self, cid, secret, session=None):
        self.s = session or requests.Session()
        self.h = {'X-NCP-APIGW-API-KEY-ID': cid, 'X-NCP-APIGW-API-KEY': secret, 'Content-Type': 'application/json'}
        self.calls = 0

    def lab(self, cat, keywords, start, end):
        body = {'startDate': start, 'endDate': end, 'timeUnit': 'month', 'category': cat,
                'keyword': [{'name': k, 'param': [k]} for k in keywords]}
        for attempt in range(4):
            self.calls += 1
            time.sleep(0.15)
            r = self.s.post(LAB_URL, headers=self.h, data=json.dumps(body, ensure_ascii=False).encode(), timeout=20)
            if r.status_code == 429:
                time.sleep(2 * (attempt + 1)); continue
            if r.status_code in (401, 403):
                raise RuntimeError('API HUB 인증 실패(HTTP %d): Client ID/Secret 또는 쇼핑 인사이트 사용 신청을 확인해 주세요 — %s'
                                   % (r.status_code, r.text[:200]))
            r.raise_for_status()
            return r.json()
        raise RuntimeError('API HUB 호출 한도 초과')


def popularity(api, items, now):
    end = now.replace(day=1) - datetime.timedelta(days=1)                  # 지난달 말
    start = (end.replace(day=1) - datetime.timedelta(days=62)).replace(day=1)  # 3개월 전 1일
    s, e = start.strftime('%Y-%m-%d'), end.strftime('%Y-%m-%d')
    groups = {}
    for it in items:
        cat = NAVER_CAT.get(it['c'])
        kw = short_kw(it.get('q') or it['n'])
        if cat and kw and kw != ANCHOR[cat]:
            groups.setdefault(cat, {}).setdefault(kw, []).append(it['id'])
    score = {}
    for cat, kws in groups.items():
        anchor = ANCHOR[cat]
        names = list(kws)
        for b in range(0, len(names), 4):
            batch = names[b:b + 4]
            data = api.lab(cat, [anchor] + batch, s, e)
            avg = {}
            for r in data.get('results', []):
                vals = [float(d.get('ratio', 0)) for d in r.get('data', [])]
                avg[r.get('title')] = sum(vals) / len(vals) if vals else 0.0
            base = avg.get(anchor) or 0.0
            if base <= 0:
                continue
            for kw in batch:
                v = avg.get(kw, 0.0) / base
                for iid in kws[kw]:
                    score[iid] = max(score.get(iid, 0.0), v)
        log('  %s: 검색어 %d개 확인' % (cat, len(names)))
    return score


def main(argv=None, api=None, now=None):
    argv = argv if argv is not None else sys.argv[1:]
    pw = os.environ.get('HM_PW')
    now = now or (datetime.datetime.utcnow() + datetime.timedelta(hours=9))
    meta = json.load(open(os.path.join(DATA, 'meta.json')))
    aes = make_key(pw, base64.b64decode(meta['salt']), meta['iter'])
    prod = dec(aes, os.path.join(DATA, 'p.bin'))
    by_id = {i['id']: i for i in prod['items']}
    rec_path = os.path.join(DATA, 'rec.bin')
    rec = dec(aes, rec_path) if os.path.exists(rec_path) else {}
    rec.setdefault('top', []); rec.setdefault('price', {})
    changed = False

    # ---- 수동 가격 파일 반영 ----
    if '--prices' in argv:
        pf = argv[argv.index('--prices') + 1]
        mp = json.load(open(pf, encoding='utf-8'))
        rec['price'] = {i: v for i, v in mp['items'].items() if i in by_id}
        rec['price_date'] = mp.get('date', '')
        rec['price_mall'] = mp.get('mall', '')
        rec['price_note'] = mp.get('note', '')
        log('수동 가격 %d개 반영 (%s %s)' % (len(rec['price']), rec['price_mall'], rec['price_date']))
        changed = True

    # ---- 인기도 (API HUB 쇼핑인사이트) ----
    cid, sec = os.environ.get('NAVER_CLIENT_ID'), os.environ.get('NAVER_CLIENT_SECRET')
    if api or (cid and sec):
        api = api or Hub(cid, sec)
        items = [i for i in prod['items'] if i['c'] not in SKIP_CATS and not i['n'].startswith('규격(')]
        log('인기도 확인 대상', len(items), '개')
        score = popularity(api, items, now)
        ranked = sorted((i for i in score if score[i] > 0), key=lambda i: -score[i])
        top, per = [], {}
        for iid in ranked:
            c = by_id[iid]['c']
            if per.get(c, 0) >= PER_CAT:
                continue
            per[c] = per.get(c, 0) + 1
            top.append(iid)
            if len(top) >= TOP_N:
                break
        rec['top'] = top
        rec['pop_date'] = now.strftime('%Y-%m-%d')
        rec['basis'] = '네이버 쇼핑 클릭량'
        log('인기 상품 %d개 선정 · API 호출 %d회' % (len(top), api.calls))
        for i in top:
            log('  - %s · %s · %.3f' % (by_id[i]['c'], by_id[i]['n'], score[i]))
        changed = True
    else:
        log('NAVER_CLIENT_ID / NAVER_CLIENT_SECRET 이 없어서 인기도 확인은 건너뛰어요 (기존 목록 유지)')

    if not changed:
        return 0
    # 판매 종료된 상품 정리
    rec['top'] = [i for i in rec['top'] if i in by_id]
    rec['price'] = {i: v for i, v in rec['price'].items() if i in by_id}
    rec['updated'] = now.strftime('%Y-%m-%d')
    enc(aes, rec, rec_path)
    meta['ver'] = int(time.time())
    json.dump(meta, open(os.path.join(DATA, 'meta.json'), 'w'))
    summary = '인기 %d개 · 가격 비교 %d개 · 기준일 %s' % (len(rec['top']), len(rec['price']), rec['updated'])
    log(summary)
    gs = os.environ.get('GITHUB_STEP_SUMMARY')
    if gs:
        with open(gs, 'a') as f:
            f.write('### 황금마차 이번 달 추천\n\n' + summary + '\n\n' +
                    '\n'.join('- %s · %s' % (by_id[i]['c'], by_id[i]['n']) for i in rec['top']) + '\n')
    return 0


if __name__ == '__main__':
    try:
        sys.exit(main())
    except Exception as e:
        log('추천 실패:', e)
        sys.exit(1)
