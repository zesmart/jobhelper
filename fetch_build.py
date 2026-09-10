#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一体化构建脚本：抓取原站 -> 解析 RAW_DATA -> 关键词打分 -> 生成静态检索页
纯标准库，Python 3.8+ 可直接运行，无第三方依赖。

用法：
    python fetch_build.py                 # 抓取并生成 dist/
    SRC_URL=http://... python fetch_build.py   # 自定义数据源

退出码：0 成功；非 0 表示失败（CI 会据此中止，不会覆盖已发布内容）
"""
import gzip
import json
import os
import re
import sys
import time
import urllib.request
from collections import Counter
from datetime import datetime, timedelta, timezone

SRC_URL = os.environ.get(
    'SRC_URL', 'http://101.132.173.68/campus/campus_recruit.html')
BASE = os.path.dirname(os.path.abspath(__file__))
DIST = os.path.join(BASE, 'dist')
CST = timezone(timedelta(hours=8))
MIN_ROWS = 5000          # 低于此条数视为抓取异常，拒绝发布
UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/124.0 Safari/537.36')


def log(*a):
    print(f'[{datetime.now(CST):%H:%M:%S}]', *a, flush=True)


# ---------------------------------------------------------------- 1. 抓取
def fetch(url, tries=3, timeout=90):
    last = None
    for i in range(tries):
        try:
            req = urllib.request.Request(
                url, headers={'User-Agent': UA, 'Accept-Encoding': 'gzip'})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                if r.headers.get('Content-Encoding') == 'gzip':
                    raw = gzip.decompress(raw)
                txt = raw.decode('utf-8', errors='replace')
                log(f'抓取成功 {len(txt)/1048576:.2f} MB')
                return txt
        except Exception as e:                       # noqa: BLE001
            last = e
            log(f'抓取失败 {i+1}/{tries}: {e}')
            if i + 1 < tries:
                time.sleep(8)
    raise SystemExit(f'❌ 抓取失败，已重试 {tries} 次：{last}')


# ------------------------------------------------------- 2. 提取内联数据
def grab_balanced(s, marker):
    """从 marker 之后提取一个完整的 JSON 数组 / 对象（括号配对，跳过字符串内容）"""
    i = s.find(marker)
    if i < 0:
        return None
    p = i + len(marker)
    while p < len(s) and s[p] not in '[{':
        p += 1
    if p >= len(s):
        return None
    open_ch = s[p]
    close_ch = '}' if open_ch == '{' else ']'
    depth, in_str, esc = 0, False, False
    for k in range(p, len(s)):
        ch = s[k]
        if in_str:
            if esc:
                esc = False
            elif ch == '\\':
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return s[p:k + 1]
    return None


# --------------------------------------------------------- 3. 关键词打分
CORE = ['物流', '供应链', '仓储', '仓配', '仓库', '运输', '配送', '快递', '关务', '报关',
        '货运', '船务', '海运', '空运', '航运', '单证', '货代', '冷链', '理货', '分拨',
        '转运', '干线', '履约', '物料', '库存', '调度', '采购', '供应商', '货源', '寻源',
        'sourcing', 'PMC', '物流管理', '物流工程', '交通运输', '供应链管理', '采购管理',
        '仓储管理', '运输管理', '配送中心', '物资管理', '合同物流', '三方物流', '国际物流']
RELA = ['计划', '订单', '贸易', '进出口', '物资', '盘点', '调拨', '发货', '收货',
        '装卸', '运力', '港口', '码头', '集运', '包装', '质检', '精益', '生产运营',
        '物流类', '运营规划', '网络规划', '运营管理', '仓网', '选址', '车辆']
COMPANY_HINT = ['物流', '供应链', '仓储', '运输', '快递', '货运', '港口', '航运',
                '船务', '货代', '顺丰', '京东物流', '菜鸟', '中通', '圆通', '韵达',
                '申通', '德邦', '跨越', '邮政', '外运', '中外运', '中远海', '招商局']
SPLIT = re.compile(r'[,，、/;；|]+')


def score(rec):
    pos = rec.get('positions') or ''
    comp = rec.get('company') or ''
    eva = rec.get('evaluation') or ''
    ind = rec.get('industry') or ''
    hits, s, matched = set(), 0, []
    for kw in CORE:
        if kw in pos:
            s += 10
            hits.add(kw)
    for kw in RELA:
        if kw in pos:
            s += 3
            hits.add(kw)
    if ind == '交通/物流':
        s += 8
        hits.add('行业:交通/物流')
    for kw in COMPANY_HINT:
        if kw in comp:
            s += 6
            hits.add('公司:' + kw)
            break
    for kw in ['物流', '供应链', '仓储', '运输', '快递', '货运', '配送']:
        if kw in eva:
            s += 2
            hits.add('简介:' + kw)
    for item in (x.strip() for x in SPLIT.split(pos)):
        if not item:
            continue
        if any(k in item for k in CORE):
            matched.append(item)
        elif s >= 6 and any(k in item for k in RELA):
            matched.append(item)
    return s, sorted(hits), matched


# ------------------------------------------------------- 4. 城市省份裁剪
CITY_SUF = ['彝族自治州', '白族自治州', '土家族苗族自治州', '布依族苗族自治州',
            '苗族侗族自治州', '藏族自治州', '柯尔克孜自治州', '黎族自治县',
            '自治州', '自治区', '地区', '盟', '省', '市', '县', '区']


def norm_city(c):
    s = (c or '').strip().rstrip('.').strip()
    for x in CITY_SUF:
        if len(s) > 2 and s.endswith(x):
            s = s[:-len(x)]
            break
    return s if len(s) >= 2 else ''


# ------------------------------------------------- 5. API 分片（前后端分离）
def gzsize(path, level=6):
    """返回文件 gzip 后的字节数——托管平台会自动压缩，这才是真实传输体积"""
    with open(path, 'rb') as f:
        return len(gzip.compress(f.read(), level))


def write_api(out, city_prov, stamp, strong):
    """产出一组静态 JSON 接口，前端按需拉取，从而与数据解耦。

    dist/api/
      meta.json       版本与分片清单（约 1KB，首屏先拉它判断是否需要更新）
      all.json        全量岗位
      logistics.json  物流 / 供应链命中分包（体积约为全量的 1/4）
      cities.json     城市 -> 省份映射
    """
    api = os.path.join(DIST, 'api')
    os.makedirs(api, exist_ok=True)

    def dump(name, obj):
        p = os.path.join(api, name)
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(obj, f, ensure_ascii=False, separators=(',', ':'))
        return os.path.getsize(p)

    hits = [r for r in out if r['_s'] > 0]
    b_all = dump('all.json', out)
    b_hit = dump('logistics.json', hits)
    dump('cities.json', city_prov)

    now = datetime.now(CST)
    week_ago = (now - timedelta(days=7)).strftime('%Y-%m-%d')
    today_str = now.strftime('%Y-%m-%d')
    recent = Counter(r['d'] for r in out if r['d'] >= week_ago)

    meta = {
        'updated': stamp,
        'updatedAt': now.isoformat(timespec='seconds'),
        'total': len(out),
        'hit': len(hits),
        'strong': strong,
        'cities': len(city_prov),
        'today': sum(1 for r in out if r['d'] == today_str),
        'recent7': [{'date': d, 'count': c}
                    for d, c in sorted(recent.items(), reverse=True)],
        'topIndustry': [{'name': n, 'count': c} for n, c in
                        Counter(r['i'] for r in out if r['i']).most_common(8)],
        'shards': {
            'all': {'file': 'all.json', 'count': len(out), 'bytes': b_all},
            'logistics': {'file': 'logistics.json', 'count': len(hits),
                          'bytes': b_hit},
        },
    }
    for v in meta['shards'].values():
        v['gzip'] = gzsize(os.path.join(api, v['file']))
    dump('meta.json', meta)

    g = meta['shards']['logistics']['gzip'] / 1048576
    a = meta['shards']['all']['gzip'] / 1048576
    log(f'API 分片：全量 gzip {a:.2f}MB · 物流分包 gzip {g:.2f}MB · 今日新增 {meta["today"]} 条')


def main():
    log('数据源:', SRC_URL)
    html = fetch(SRC_URL)

    log('解析 RAW_DATA ...')
    raw = grab_balanced(html, 'const RAW_DATA = ')
    if not raw:
        raise SystemExit('❌ 未找到 RAW_DATA，页面结构可能已变化')
    data = json.loads(raw)
    log(f'解析到 {len(data)} 条岗位')
    if len(data) < MIN_ROWS:
        raise SystemExit(f'❌ 仅解析到 {len(data)} 条，少于阈值 {MIN_ROWS}，判定为异常，终止')

    log('解析 CITY_PROVINCE_MAP ...')
    cp_raw = grab_balanced(html, 'const CITY_PROVINCE_MAP = ')
    full_map = json.loads(cp_raw) if cp_raw else {}
    if not full_map:
        raise SystemExit('❌ 未找到 CITY_PROVINCE_MAP，城市筛选将失效，终止')
    cities = set()
    for r in data:
        for c in re.split(r'[,，、/\\|; ]+', r.get('location') or ''):
            if c.strip():
                cities.add(c.strip())
    keep = set()
    for c in cities:
        n = norm_city(c)
        for cand in (c, n, c + '市'):
            if cand in full_map:
                keep.add(cand)
                break
    city_prov = {k: v for k, v in full_map.items() if k in keep}
    log(f'城市映射：全量 {len(full_map)} -> 实际使用 {len(city_prov)}')

    log('关键词打分 ...')
    out = []
    for r in data:
        s, hits, matched = score(r)
        out.append({
            'd': r.get('fullDate') or r.get('updateDate') or '',
            'c': r.get('company') or '',
            'i': r.get('industry') or '',
            'n': r.get('nature') or '',
            'b': r.get('batch') or '',
            'l': r.get('location') or '',
            'p': r.get('positions') or '',
            'a': r.get('appLink') or '',
            's': r.get('sourceLink') or '',
            'e': r.get('evaluation') or '',
            'dl': r.get('deadline') or '',
            '_s': s, '_h': hits,
            '_m': ' / '.join(dict.fromkeys(matched))[:200],
        })
    strong = sum(1 for r in out if r['_s'] >= 10)
    log(f'命中 {sum(1 for r in out if r["_s"] > 0)} 条，其中强相关 {strong} 条')

    os.makedirs(DIST, exist_ok=True)
    stamp = datetime.now(CST).strftime('%Y-%m-%d %H:%M')

    def esc(s):
        return s.replace('</', '<\\/')          # 防止提前闭合 <script>

    data_json = esc(json.dumps(out, ensure_ascii=False, separators=(',', ':')))
    cp_json = esc(json.dumps(city_prov, ensure_ascii=False, separators=(',', ':')))

    tpl_path = os.path.join(BASE, 'template.html')
    tpl = open(tpl_path, encoding='utf-8').read()

    # A. 内嵌版：数据烧进 HTML，双击即可离线使用
    page = (tpl.replace('__DATA__', data_json)
               .replace('__CITYPROV__', cp_json)
               .replace('__API__', "''")
               .replace('__TOTAL__', str(len(out)))
               .replace('__UPDATED__', stamp))

    # B. 接口版：页面不含数据，运行时从 dist/api/ 拉取（前后端分离）
    app = (tpl.replace('__DATA__', '[]')
              .replace('__CITYPROV__', '{}')
              .replace('__API__', "'./api/'")
              .replace('__TOTAL__', '—')
              .replace('__UPDATED__', stamp))

    for name, txt in (('index.html', page), ('app.html', app)):
        if '__DATA__' in txt or '__UPDATED__' in txt or '__API__' in txt:
            raise SystemExit(f'❌ {name} 模板占位符未被完全替换')
        with open(os.path.join(DIST, name), 'w', encoding='utf-8') as f:
            f.write(txt)
    with open(os.path.join(DIST, 'data_min.json'), 'w', encoding='utf-8') as f:
        f.write(json.dumps(out, ensure_ascii=False, separators=(',', ':')))
    with open(os.path.join(DIST, 'city_prov.json'), 'w', encoding='utf-8') as f:
        f.write(json.dumps(city_prov, ensure_ascii=False, separators=(',', ':')))
    with open(os.path.join(DIST, 'meta.json'), 'w', encoding='utf-8') as f:
        json.dump({'updated': stamp, 'total': len(out), 'strong': strong,
                   'cities': len(city_prov),
                   'topIndustry': Counter(r['i'] for r in out if r['i']).most_common(8)},
                  f, ensure_ascii=False, indent=1)
    open(os.path.join(DIST, '.nojekyll'), 'w').close()

    write_api(out, city_prov, stamp, strong)

    size = os.path.getsize(os.path.join(DIST, 'index.html')) / 1048576
    log(f'✅ 生成完成：dist/index.html  {size:.2f} MB  更新时间 {stamp}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
