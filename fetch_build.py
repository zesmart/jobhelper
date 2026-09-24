#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一体化构建脚本：抓取原站 -> 解析岗位数据 -> 关键词打分 -> 生成静态检索页
纯标准库，Python 3.8+ 可直接运行，无第三方依赖。

数据源两种形态都吃得下：
    新版  页面只下发 META 骨架，数据在 data/pN.<日期>.json 批次里（2026-09-24 起）
    旧版  数据内嵌在页面的 const RAW_DATA = [...] 里
离线复现：LOCAL_ROWS=<快照> 走本地快照，完全不联网。

用法：
    python fetch_build.py                      # 抓取并生成 dist/
    SRC_URL=http://... python fetch_build.py   # 自定义数据源
    LOCAL_ROWS=snap.json python fetch_build.py # 用本地快照构建

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
from urllib.parse import urljoin

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
def grab_json_from(s, p):
    """从位置 p 起找到第一个 [ 或 {，按括号配对抠出完整 JSON（跳过字符串内容）"""
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


def grab_balanced(s, marker):
    """从 marker 之后提取一个完整的 JSON 数组 / 对象"""
    i = s.find(marker)
    if i < 0:
        return None
    return grab_json_from(s, i + len(marker))


def grab_by_re(s, pattern):
    """按正则定位再抠 JSON —— 声明前缀可能是 const / let，写死 marker 太脆"""
    m = re.search(pattern, s)
    if not m:
        return None
    # 正则若已经把开括号吃进匹配（...=\s*\{），要回退一格，
    # 否则会从括号「里面」开始找下一个括号，抠出内层数组（踩过：META 抠成 files）
    p = m.end()
    if p > 0 and s[p - 1] in '[{':
        p -= 1
    return grab_json_from(s, p)


# ------------------------------------------- 2b. 新版源站：骨架 + 批次分包
# ⚠️ 2026-09-24 源站改版：页面不再内嵌全量数据（原来是 const RAW_DATA = [...]，页面 4.6MB），
#    改成只下发 META 骨架（含批次清单），数据拆成 data/pN.<日期>.json 由前端按需 fetch。
#    旧锚点在新页面里只剩一句 `let RAW_DATA = [];`（空数组）—— 于是抓取脚本在
#    「找不到 RAW_DATA」上直接崩，这就是 9/24 两次同步全挂的原因。
META_DECL = re.compile(r'(?:const|let|var)\s+META\s*=\s*\{')


def fetch_batches(meta, page_url):
    """按 META.files 逐个拉批次再拼接。批次按日期从新到旧切，拼接顺序即源站顺序。"""
    files = meta.get('files') or []
    if not files:
        return []
    rows = []
    for i, name in enumerate(files, 1):
        u = urljoin(page_url, 'data/' + str(name) + '.json')
        arr = json.loads(fetch(u))
        if not isinstance(arr, list):
            raise SystemExit(f'❌ 批次 {name} 不是数组，源站结构可能又变了')
        rows.extend(arr)
        log(f'  批次 {i}/{len(files)} {name}：{len(arr)} 条（累计 {len(rows)}）')
    return rows


def parse_page_rows(html, page_url):
    """取出岗位数组。新版走 META + 批次分包，旧版回退内联 RAW_DATA。"""
    meta_raw = grab_by_re(html, META_DECL)
    if meta_raw:
        try:
            meta = json.loads(meta_raw)
        except Exception as e:                       # noqa: BLE001
            raise SystemExit(f'❌ META 解析失败：{e}')
        files = meta.get('files') or []
        log(f'页面是新版骨架：{len(files)} 个批次 · 声明 total={meta.get("total")}')
        if files:
            rows = fetch_batches(meta, page_url)
            # META.total 把商业化条目也数进去了，而那些条目只随骨架下发（META.comm）、
            # 不进批次 —— 所以批次合计天然比 total 少 comm 的条数，这是预期，不是异常。
            comm = meta.get('comm') or []
            want = meta.get('total')
            if want:
                expect = want - len(comm)
                if len(rows) == expect:
                    log(f'META.total={want} 里有 {len(comm)} 条商业化（不随批次下发），'
                        f'批次净得 {len(rows)} 条 ✓')
                else:
                    log(f'⚠️ 批次 {len(rows)} 条，按 total-comm 应为 {expect} 条，'
                        f'差 {len(rows) - expect}')
            return rows
        log('⚠️ META 里没有 files，回退旧版内联解析')

    log('按旧版内联结构解析 RAW_DATA ...')
    raw = grab_balanced(html, 'const RAW_DATA = ')
    if not raw:
        raise SystemExit('❌ 新版 META 与旧版 RAW_DATA 都没抠到，源站结构可能又变了')
    return json.loads(raw)


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

# ------------------------------------------------ 正式版（通用求职工具）配置
# 热门岗位关键词取自全站岗位字段的实际覆盖率统计（工程师 34.7%、研发 25.4%、
# 开发 24.0%、算法 21.8%、产品 21.6%、运营 19.2% …），不含任何行业限定词。
HOT_KEYWORDS = ['工程师', '研发', '开发', '算法', '设计', '产品', '运营', '销售', '测试',
                '市场', '财务', '机械', '硬件', '电气', '工艺', '管培生', '实习', '质量',
                '人力', '行政']

# 正式版发布目录：与其他版本分开存放，整个目录可直接部署到任意静态托管
WEB = os.path.abspath(os.environ.get('WEB_OUT')
                      or os.path.join(BASE, '..', 'web'))

# 接口的默认云端回退地址：本地以 file:// 双击打开时无法 fetch 相对路径 ./api/，
# 必须注入一个绝对地址才连得上；留空会导致页面直接判「未找到数据源」。
# 同域部署（相对 ./api/ 可用）时不会走到这里；换托管平台用 WEB_API / PAGES_API 覆盖。
DEFAULT_CLOUD_API = 'https://zesmart.github.io/jobhelper/api/'

# AI 智选代理的线上地址（已发布的 Node 服务，持有大模型 Key）。
# 跟数据接口一样给默认值：否则每次重建都会静默把 AI 功能丢掉，
# 页面上只留一句「本页没有接入 AI 服务」，很难被发现。
# 代理换地址 / 换平台时用 AI_PROXY_URL 覆盖。
DEFAULT_AI_PROXY = 'https://jobhelper-ai-proxy.app.workbuddy.host'

# 物流专版的前端快捷词与预设词（与上面的 CORE/RELA 用途不同：那两个用于数据打分）
LOGISTICS_CHIPS = ['物流', '供应链', '仓储', '运输', '配送', '采购', '供应商', '关务', '报关',
                   '单证', '货运', '船务', '快递', '物料', '库存', '调度', '履约', 'PMC',
                   '管培生', '物流管理']
LOGISTICS_CORE = ['物流', '供应链', '仓储', '仓配', '仓库', '运输', '配送', '快递', '关务', '报关',
                  '货运', '船务', '海运', '空运', '航运', '单证', '货代', '冷链', '理货', '分拨',
                  '转运', '干线', '履约', '物料', '库存', '调度', '采购', '供应商', '货源', '寻源',
                  'PMC', '物流管理', '物流工程', '交通运输', '供应链管理', '采购管理', '仓储管理',
                  '运输管理', '配送中心', '物资管理', '国际物流']
LOGISTICS_BROAD = LOGISTICS_CORE + ['计划', '订单', '贸易', '进出口', '物资', '盘点', '调拨',
                                    '发货', '收货', '装卸', '运力', '港口', '码头', '集运',
                                    '包装', '精益', '生产运营', '物流类', '运营规划',
                                    '网络规划', '运营管理', '仓网', '车辆']


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


def write_web_api(out, city_prov, stamp):
    """正式版接口：只暴露全量分片，并剔除内部打分字段（_s / _h / _m）。

    web/api/
      meta.json    版本与总条数
      all.json     全量岗位
      cities.json  城市 -> 省份映射
    """
    api = os.path.join(WEB, 'api')
    os.makedirs(api, exist_ok=True)

    def dump(name, obj):
        p = os.path.join(api, name)
        with open(p, 'w', encoding='utf-8') as f:
            json.dump(obj, f, ensure_ascii=False, separators=(',', ':'))
        return os.path.getsize(p)

    rows = [{k: v for k, v in r.items() if not k.startswith('_')} for r in out]
    b_all = dump('all.json', rows)
    dump('cities.json', city_prov)

    now = datetime.now(CST)
    week_ago = (now - timedelta(days=7)).strftime('%Y-%m-%d')
    today_str = now.strftime('%Y-%m-%d')
    shard = {'file': 'all.json', 'count': len(rows), 'bytes': b_all}
    shard['gzip'] = gzsize(os.path.join(api, 'all.json'))

    meta = {
        'updated': stamp,
        'updatedAt': now.isoformat(timespec='seconds'),
        'total': len(rows),
        'companies': len({r['c'] for r in rows}),
        'cities': len(city_prov),
        'today': sum(1 for r in rows if r['d'] == today_str),
        'recent7': [{'date': d, 'count': c} for d, c in sorted(
            Counter(r['d'] for r in rows if r['d'] >= week_ago).items(), reverse=True)],
        'topIndustry': [{'name': n, 'count': c} for n, c in
                        Counter(r['i'] for r in rows if r['i']).most_common(8)],
        'shards': {'all': shard},
    }
    dump('meta.json', meta)
    log(f'正式版接口：{len(rows)} 条 · gzip {shard["gzip"]/1048576:.2f}MB · '
        f'公司 {meta["companies"]}')


# ------------------------------------------------- 6. 模板渲染（双版本共用）
PLACEHOLDERS = ['TITLE', 'BRANDTAG', 'H1', 'UPDATED', 'HEROSUB', 'KWPLACEHOLDER', 'SEARCHBTNS',
                'CHIPS', 'SORTOPTS', 'SORTDEFAULT', 'HOWTO', 'DATA', 'CITYPROV', 'PAGES', 'API',
                'PRESET_CORE', 'PRESET_BROAD', 'SHARDDEFAULT', 'BOOTPRESET', 'AIPROXY', 'AUTHAPI',
                'JOBSAPI']


def chips_html(words):
    return '\n'.join(f'      <span class="chip" data-k="{w}">{w}</span>' for w in words)


def jslit(obj):
    return json.dumps(obj, ensure_ascii=False, separators=(',', ':'))


def render(tpl, cfg):
    """按配置替换占位符；缺配置或有残留都直接报错，避免产出半成品"""
    missing = [k for k in PLACEHOLDERS if k not in cfg]
    if missing:
        raise SystemExit(f'❌ 渲染配置缺少占位符：{missing}')
    page = tpl
    for k in PLACEHOLDERS:
        page = page.replace(f'__{k}__', cfg[k])
    # 全量扫描一次：任何漏配的占位符都会在这里暴露，而不是留到线上
    left = sorted(set(re.findall(r'__[A-Z][A-Z0-9_]*__', page)))
    if left:
        raise SystemExit(f'❌ 占位符未被替换：{left}')
    return page


HOWTO_LOGISTICS = """        <p><b>精准筛选</b>：点「物流供应链精准」一键筛出对口岗位；也可自己输入关键词，空格分隔表示满足任一即可，勾选「需同时包含全部词」则要求全部命中。</p>
        <p><b>高亮</b>：命中的关键词会以<em>橙色</em>标出，方便一眼确认这家公司招的到底是不是对口岗位。</p>
        <p><b>城市多选</b>：点「全部城市」展开面板，可按省份整选、搜索城市或直接点热门城市。很多岗位标注「全国 / 海外」，默认纳入结果；想只看明确写了具体城市的，取消勾选「含全国 / 海外岗位」。</p>
        <p><b>看全部岗位</b>：卡片默认只显示命中的岗位，点「展开全部岗位」可查看该公司的完整在招列表。</p>"""

HOWTO_PUBLIC = """        <p><b>搜索</b>：输入岗位关键词即可筛选，空格分隔表示满足任一条件；勾选「需同时包含全部词」则要求全部命中。也可以直接点上方快捷词。</p>
        <p><b>高亮</b>：命中的关键词会以<em>橙色</em>标出，方便快速定位。</p>
        <p><b>城市多选</b>：点「全部城市」展开面板，可按省份整选、搜索城市或直接点热门城市。很多岗位标注「全国 / 海外」，默认纳入结果；想只看明确写了具体城市的，取消勾选「含全国 / 海外岗位」。</p>
        <p><b>排序</b>：默认按更新时间倒序，最新发布的岗位排在最前；也可切换为按相关度或公司名排序。</p>
        <p><b>看全部岗位</b>：卡片默认只展示部分岗位，点「展开全部岗位」可查看该公司的完整在招列表。</p>"""


def main():
    # 离线快照入口：LOCAL_ROWS 指向一份「源站原始长键格式」的 JSON（_grab_src.py 产出）。
    # 有它就完全不联网，构建可复现；没有才去抓源站。
    snap = os.environ.get('LOCAL_ROWS')
    html = ''
    if snap and os.path.exists(snap):
        log('数据源: 本地快照', snap)
        data = json.load(open(snap, encoding='utf-8'))
        log(f'从快照读取到 {len(data)} 条岗位')
    else:
        log('数据源:', SRC_URL)
        html = fetch(SRC_URL)

        log('解析岗位数据 ...')
        data = parse_page_rows(html, SRC_URL)
        log(f'解析到 {len(data)} 条岗位')
    if len(data) < MIN_ROWS:
        raise SystemExit(f'❌ 仅解析到 {len(data)} 条，少于阈值 {MIN_ROWS}，判定为异常，终止')

    log('解析 CITY_PROVINCE_MAP ...')
    cp_raw = grab_balanced(html, 'const CITY_PROVINCE_MAP = ') if html else None
    if not cp_raw:
        # 离线快照模式：城市映射单独存一份 _src_raw_cp.json（_grab_src.py 产出）
        cp_file = os.path.splitext(snap)[0] + '_cp.json' if snap else ''
        if cp_file and os.path.exists(cp_file):
            full_map = json.load(open(cp_file, encoding='utf-8'))
            log(f'从快照读取城市映射 {len(full_map)} 条')
        else:
            raise SystemExit('❌ 未找到 CITY_PROVINCE_MAP，城市筛选将失效，终止')
    else:
        full_map = json.loads(cp_raw)
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
    os.makedirs(WEB, exist_ok=True)
    stamp = datetime.now(CST).strftime('%Y-%m-%d %H:%M')

    def esc(s):
        return s.replace('</', '<\\/')          # 防止提前闭合 <script>

    data_json = esc(json.dumps(out, ensure_ascii=False, separators=(',', ':')))
    cp_json = esc(json.dumps(city_prov, ensure_ascii=False, separators=(',', ':')))

    tpl = open(os.path.join(BASE, 'template.html'), encoding='utf-8').read()

    # 本地以 file:// 双击打开时无法 fetch 相对路径 ./api/，必须注入绝对地址回退，
    # 否则页面会直接判「未找到数据源」、连一次请求都不发。故两者都给默认云端地址。
    def _api_env(name, default):
        v = os.environ.get(name, '').strip() or default
        return v if v.endswith('/') else v + '/'

    # 物流版的回退地址；换托管域名时用 PAGES_API 覆盖
    #   Windows:  set PAGES_API=https://<域名>/api/ && python fetch_build.py
    #   macOS/Linux: PAGES_API=https://<域名>/api/ python fetch_build.py
    pages_api = _api_env('PAGES_API', DEFAULT_CLOUD_API)
    pages_js = f"'{pages_api}'"

    # 正式版独立于上面的托管地址：将来换域名 / 换平台时用 WEB_API 覆盖
    #   set WEB_API=https://<你的域名>/api/ && python fetch_build.py
    web_api = _api_env('WEB_API', DEFAULT_CLOUD_API)
    web_api_js = f"'{web_api}'"

    # AI 智选的服务端代理地址。大模型 Key 由代理持有，**绝不能**出现在前端，
    # 所以这里只注入一个匿名地址（即便被人看到，它也只认四个字段、且有限流和配额）。
    # 默认指向已发布的代理；换地址时用 AI_PROXY_URL 覆盖，或用 AI_PROXY_URL=off 显式关闭。
    #   set AI_PROXY_URL=https://<代理域名> && python fetch_build.py
    ai_proxy = os.environ.get('AI_PROXY_URL', '').strip().rstrip('/')
    if ai_proxy.lower() in ('off', 'none', 'disable'):
        ai_proxy = ''
    elif not ai_proxy:
        ai_proxy = DEFAULT_AI_PROXY
    ai_proxy_js = f"'{ai_proxy}'" if ai_proxy else "''"
    # 登录用的也是同一个代理（校园统一身份认证不支持跨域，必须转一层），
    # 所以跟着 AI 代理一起开关：AI_PROXY_URL=off 时登录也一起关掉。
    auth_proxy_js = ai_proxy_js
    # 通用岗位页的聚合接口同样不支持跨域、且密钥不能进前端，也走同一个代理。
    # 想单独指向别处时用 JOBS_API_URL 覆盖，JOBS_API_URL=off 可单独关掉通用页。
    jobs_api = os.environ.get('JOBS_API_URL', '').strip().rstrip('/')
    if jobs_api.lower() in ('off', 'none', 'disable'):
        jobs_api_js = "''"
    elif jobs_api:
        jobs_api_js = f"'{jobs_api}'"
    else:
        jobs_api_js = ai_proxy_js

    n_total, n_comp = len(out), len({r['c'] for r in out})
    SORTOPTS_LOGISTICS = ('<option value="score" selected>相关度优先</option>'
                          '<option value="date">更新日期</option>'
                          '<option value="name">公司名</option>')
    SORTOPTS_PUBLIC = ('<option value="date" selected>更新时间优先</option>'
                       '<option value="score">相关度优先</option>'
                       '<option value="name">公司名</option>')
    # 「清空」已内置在输入框内（由模板提供 inp-clear），这里不再注入
    BTNS_LOGISTICS = """<button class="btn solid" onclick="preset('core',1)">物流供应链精准</button>
      <button class="btn" onclick="preset('broad',1)">泛相关</button>"""

    # ---- 版本 A：物流专版（沿用既有定位，供对口求职者使用）----
    logi = {
        'TITLE': '求职助手 · 物流 / 供应链岗位检索',
        'BRANDTAG': '<span class="brand-tag">物流 / 供应链</span>',
        'H1': '找到属于你的物流 / 供应链岗位',
        'HEROSUB': (f'共收录 <b class="num" id="hsAll">{n_total}</b> 条校招信息 · '
                    f'已按物流、供应链、仓储、采购等关键词预筛 · 数据每日自动同步'),
        'KWPLACEHOLDER': '输入岗位关键词，如：物流 供应链 采购 关务（空格分隔多个词）',
        'SEARCHBTNS': BTNS_LOGISTICS,
        'CHIPS': chips_html(LOGISTICS_CHIPS),
        'SORTOPTS': SORTOPTS_LOGISTICS,
        'SORTDEFAULT': '相关度优先',
        'HOWTO': HOWTO_LOGISTICS,
        'PRESET_CORE': jslit(LOGISTICS_CORE),
        'PRESET_BROAD': jslit(LOGISTICS_BROAD),
        'SHARDDEFAULT': "'logistics'",
        'BOOTPRESET': "'core'",
        'AIPROXY': ai_proxy_js,
        'AUTHAPI': auth_proxy_js,
        'JOBSAPI': jobs_api_js,
        'UPDATED': stamp,
    }
    page = render(tpl, dict(logi, DATA=data_json, CITYPROV=cp_json,
                            API="''", PAGES="''"))
    app = render(tpl, dict(logi, DATA='[]', CITYPROV='{}',
                           API="'./api/'", PAGES=pages_js))

    # ---- 版本 B：正式版（通用求职工具，独立目录存放）----
    public = {
        'TITLE': '求职助手 · 校招岗位检索',
        'BRANDTAG': '<span class="brand-tag">27届校招</span>',
        'H1': '找到属于你的校招岗位',
        'HEROSUB': (f'共收录 <b class="num" id="hsAll">{n_total}</b> 条校招信息 · '
                    f'覆盖 <b id="hsComp">{n_comp}</b> 家企业 · 数据每日自动同步'),
        'KWPLACEHOLDER': '输入岗位关键词，如：工程师 产品 运营 财务（空格分隔多个词）',
        'SEARCHBTNS': '',   # 「清空」已内置在输入框内（模板 inp-clear）
        'CHIPS': chips_html(HOT_KEYWORDS),
        'SORTOPTS': SORTOPTS_PUBLIC,
        'SORTDEFAULT': '更新时间优先',
        'HOWTO': HOWTO_PUBLIC,
        'PRESET_CORE': jslit(HOT_KEYWORDS),
        'PRESET_BROAD': jslit(HOT_KEYWORDS),
        'SHARDDEFAULT': "'all'",
        'BOOTPRESET': "''",
        'AIPROXY': ai_proxy_js,
        'AUTHAPI': auth_proxy_js,
        'JOBSAPI': jobs_api_js,
        'UPDATED': stamp,
        'DATA': '[]',
        'CITYPROV': '{}',
        'API': "'./api/'",
        'PAGES': web_api_js,
    }
    public_page = render(tpl, public)

    for path, txt in ((os.path.join(DIST, 'index.html'), page),
                      (os.path.join(DIST, 'app.html'), app),
                      (os.path.join(WEB, 'index.html'), public_page)):
        with open(path, 'w', encoding='utf-8') as f:
            f.write(txt)
    for name, obj in (('data_min.json', out), ('city_prov.json', city_prov)):
        with open(os.path.join(DIST, name), 'w', encoding='utf-8') as f:
            f.write(json.dumps(obj, ensure_ascii=False, separators=(',', ':')))
    with open(os.path.join(DIST, 'meta.json'), 'w', encoding='utf-8') as f:
        json.dump({'updated': stamp, 'total': n_total, 'strong': strong,
                   'cities': len(city_prov),
                   'topIndustry': Counter(r['i'] for r in out if r['i']).most_common(8)},
                  f, ensure_ascii=False, indent=1)
    open(os.path.join(DIST, '.nojekyll'), 'w').close()

    write_api(out, city_prov, stamp, strong)
    write_web_api(out, city_prov, stamp)

    s1 = os.path.getsize(os.path.join(DIST, 'index.html')) / 1048576
    s2 = os.path.getsize(os.path.join(WEB, 'index.html')) / 1024
    log(f'✅ 生成完成：dist/index.html {s1:.2f} MB · web/index.html {s2:.1f} KB · 更新时间 {stamp}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
