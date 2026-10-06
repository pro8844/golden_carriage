"""황금마차 월간 자동 업데이트 (GitHub Actions에서 실행)

1) 국군복지포털 로그인 → 마트 판매상품 전체 + 2026 신규상품 목록 수집
2) 기존 암호화 데이터(data/p.bin, data/t*.bin)를 복호화해서 분류·사진 재사용
3) 새 상품은 비슷한 이름의 기존 상품 분류를 따라가고, 사진은 새로 받아 96px로 축소
4) 같은 비밀번호·같은 salt로 다시 암호화해서 data/ 갱신 (가족 휴대폰의 '기억하기' 유지)

필요한 환경변수(GitHub Secrets): WELFARE_ID, WELFARE_PW, HM_PW
"""
import os, re, io, sys, json, time, base64, html, unicodedata, datetime
import requests
from PIL import Image
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives import hashes

BASE = 'https://www.welfare.mil.kr'
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, 'data')
MIN_ITEMS = 1000          # 이보다 적게 수집되면 실패로 보고 기존 데이터 유지
UA = 'Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Mobile Safari/537.36'


def log(*a):
    print('[update]', *a, flush=True)


# ---------------- 암호화 ----------------
def make_key(pw, salt, it):
    pw = unicodedata.normalize('NFC', pw.strip())
    return AESGCM(PBKDF2HMAC(algorithm=hashes.SHA256(), length=32, salt=salt, iterations=it).derive(pw.encode()))


def dec(aes, path):
    b = open(path, 'rb').read()
    return json.loads(aes.decrypt(b[:12], b[12:], None))


def enc(aes, obj, path):
    iv = os.urandom(12)
    data = json.dumps(obj, ensure_ascii=False, separators=(',', ':')).encode()
    with open(path, 'wb') as f:
        f.write(iv + aes.encrypt(iv, data, None))


# ---------------- 수집 ----------------
ROW = re.compile(r'<tr>(.*?)</tr>', re.S)
TD = re.compile(r'<td[^>]*>(.*?)</td>', re.S)
TAG = re.compile(r'<[^>]+>')


def clean(s):
    return re.sub(r'\s+', ' ', html.unescape(TAG.sub('', s))).strip()


def parse_list(page_html):
    out = []
    for row in ROW.findall(page_html):
        tds = TD.findall(row)
        if len(tds) < 6:
            continue
        m = re.search(r'p_code=(\d+)', tds[3])
        if not m:
            continue
        price = re.sub(r'\D', '', clean(tds[5]))
        if not price:
            continue
        out.append({'id': m.group(1), 'v': clean(tds[2]), 'n': clean(tds[3]), 's': clean(tds[4]), 'p': int(price)})
    return out


def last_page(page_html):
    # 페이지 이동 링크(pg=숫자) 중 가장 큰 번호 = 마지막 페이지 ('끝' 링크 형식이 달라도 동작)
    nums = [int(x) for x in re.findall(r'[?&;]pg=(\d+)', page_html)]
    return max(nums) if nums else 1


def login(s, uid, pw):
    s.get(BASE + '/content/content.do?m_code=139&goCd=114', timeout=30)
    r = s.post(BASE + '/content/content.do?m_code=139&forwardName=login.userActionLogin',
               data={'type': 'user', 'goCd': '114', 'message': '', 'goUrl': '', 'be_id': '', 'bm_serial': '',
                     'ct': '', 'pCmV': '', 'cyber_id': uid, 'cyber_pw': pw}, timeout=30)
    r.raise_for_status()


def fetch_all(s, new_only=False):
    q = ('p_open_dt=Y&c_codex=3&c_depth=' if new_only else 'p_open_dt=&c_codex=&c_depth=null')
    url = BASE + '/content/content.do?m_code=114&type=user&paging_num=10&c_parent=0&' + q + '&pg=%d'
    first = s.get(url % 1, timeout=30).text
    if '로그아웃' not in first and not parse_list(first):
        raise RuntimeError('로그인 실패 또는 목록 접근 불가 (아이디/비밀번호 또는 사이트 변경 확인)')
    items, last = parse_list(first), last_page(first)
    pg = 2
    while pg <= max(last, 2) and pg <= 200:
        page = s.get(url % pg, timeout=30).text
        got = parse_list(page)
        if not got:
            break
        items += got
        last = max(last, last_page(page))   # 다음 묶음(11~20쪽 …) 링크로 마지막 쪽을 다시 확인
        pg += 1
        time.sleep(0.4)
    log('수집', '신규' if new_only else '전체', len(items), '개 /', last, '페이지')
    return items


def thumb(s, pid):
    r = s.get(BASE + '/shop/imgView.do?p_code=%s&type=1' % pid, timeout=30)
    im = Image.open(io.BytesIO(r.content)).convert('RGB')
    im.thumbnail((96, 96), Image.LANCZOS)
    bg = Image.new('RGB', im.size, 'white'); bg.paste(im)
    b = io.BytesIO(); bg.save(b, 'WEBP', quality=72)
    return 'data:image/webp;base64,' + base64.b64encode(b.getvalue()).decode()


# ---------------- 분류 (비슷한 이름의 기존 상품 분류를 따름) ----------------
def grams(t):
    t = re.sub(r'\((영외|동계|공군|해군)\)|규격\(\d+\)|[^0-9a-z가-힣]', '', t.lower())
    return {t[i:i + 2] for i in range(len(t) - 1)} or {t}


def guess_cat(item, known):
    if item['n'].startswith('규격(') or '규격(' in item['n'][:6]:
        return '의류·피복·신발'
    g = grams(item['n'])
    best, score = '기타', 0.0
    for k in known:
        inter = len(g & k[0])
        if not inter:
            continue
        sc = inter / len(g | k[0]) + (0.05 if k[2] == item['v'] else 0)
        if sc > score:
            best, score = k[1], sc
    return best if score >= 0.18 else '기타'


def query_name(n):
    return re.sub(r'\s+', ' ', re.sub(r'\((영외|동계|공군)\)|규격\(\d+\)', '', n)).strip()


# ---------------- 메인 ----------------
def main(scrape=None):
    meta = json.load(open(os.path.join(DATA, 'meta.json')))
    aes = make_key(os.environ['HM_PW'], base64.b64decode(meta['salt']), meta['iter'])
    old = dec(aes, os.path.join(DATA, 'p.bin'))
    cats = old['cats']
    old_by_id = {i['id']: i for i in old['items']}
    old_thumbs = {}
    for k in range(len(cats)):
        p = os.path.join(DATA, 't%d.bin' % k)
        if os.path.exists(p):
            old_thumbs.update(dec(aes, p))
    log('기존 상품', len(old_by_id), '/ 기존 사진', len(old_thumbs))

    s = requests.Session(); s.headers['User-Agent'] = UA
    if scrape:
        all_items, new_ids = scrape()
    else:
        login(s, os.environ['WELFARE_ID'], os.environ['WELFARE_PW'])
        all_items = fetch_all(s)
        new_ids = {i['id'] for i in fetch_all(s, new_only=True)}
    if len(all_items) < MIN_ITEMS:
        raise RuntimeError('수집된 상품이 %d개뿐이라 업데이트를 중단합니다' % len(all_items))

    known = [(grams(i['n']), i['c'], i['v']) for i in old['items'] if i['c'] != '기타']
    items, seen, added = [], set(), []
    for it in all_items:
        if it['id'] in seen:
            continue
        seen.add(it['id'])
        prev = old_by_id.get(it['id'])
        c = prev['c'] if prev else guess_cat(it, known)
        if c == '담배':
            continue
        if not prev:
            added.append((it['n'], c))
        items.append({'id': it['id'], 'v': it['v'], 'n': it['n'], 's': it['s'], 'p': it['p'],
                      'new': 1 if it['id'] in new_ids else 0, 'out': 1 if '(영외)' in it['n'] else 0,
                      'q': query_name(it['n']), 'c': c})
    removed = len(set(old_by_id) - seen)
    changed = sum(1 for i in items if i['id'] in old_by_id and old_by_id[i['id']]['p'] != i['p'])
    log('최종', len(items), '개 | 새 상품', len(added), '| 판매종료', removed, '| 가격변경', changed)
    for n, c in added[:30]:
        log('  +', c, '|', n)

    thumbs = {}
    for it in items:
        if it['id'] in old_thumbs:
            thumbs[it['id']] = old_thumbs[it['id']]
        elif not scrape:
            try:
                thumbs[it['id']] = thumb(s, it['id'])
            except Exception as e:
                log('  사진 실패', it['id'], e)

    today = (datetime.datetime.utcnow() + datetime.timedelta(hours=9)).strftime('%Y-%m-%d')
    out = {'updated': today, 'cats': cats, 'items': items, 'config': old.get('config', {})}
    enc(aes, out, os.path.join(DATA, 'p.bin'))
    for k, cat in enumerate(cats):
        enc(aes, {i['id']: thumbs[i['id']] for i in items if i['c'] == cat and i['id'] in thumbs},
            os.path.join(DATA, 't%d.bin' % k))
    meta['ver'] = int(time.time())
    json.dump(meta, open(os.path.join(DATA, 'meta.json'), 'w'))
    summary = '상품 %d개 (새 상품 %d, 판매종료 %d, 가격변경 %d) · 기준일 %s' % (len(items), len(added), removed, changed, today)
    log(summary)
    gs = os.environ.get('GITHUB_STEP_SUMMARY')
    if gs:
        with open(gs, 'a') as f:
            f.write('### 황금마차 데이터 업데이트\n\n' + summary + '\n\n' + '\n'.join('- %s · %s' % (c, n) for n, c in added[:50]) + '\n')


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        log('실패:', e)
        sys.exit(1)
