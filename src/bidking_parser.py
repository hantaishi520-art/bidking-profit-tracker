import re, json, csv, sys, datetime, os, mmap

RES_DIR = None  # 由 launcher 注入内嵌资源目录；为 None 时回退到脚本目录(HERE)

# =====================================================================
# BidKing 竞拍盈亏解析器（v2.34-fixed，双数据源 S2C_89 + S2C45 合并）
# 可作为脚本运行，也可被 import 调用 parse() 函数（供 serve.py / exe 使用）。
# =====================================================================

def _root_dir():
    """用户数据根目录（持久、可写），用于定位 item_prices.db 等可被用户编辑的文件。
    兼容多种运行形态：
    - 源码运行（python xxx.py）：返回脚本/模块所在目录。
    - Nuitka onefile：sys.frozen 不一定被设置，但 Windows 会把真实 exe 路径放进 sys.argv[0]，
      故优先用 argv[0] 的目录；临时解压目录(__file__ 所在)不应作为数据目录。
    - 也兼容 PyInstaller（sys.frozen + sys.executable 指向真实路径）。
    通过『目录内是否存在已知资源文件』来确认选中的目录正确，避免误判。"""
    candidates = []
    if getattr(sys, 'frozen', False):
        candidates.append(os.path.dirname(os.path.abspath(sys.executable)))
    if getattr(sys, 'argv', [''])[0]:
        candidates.append(os.path.dirname(os.path.abspath(sys.argv[0])))
    candidates.append(os.path.dirname(os.path.abspath(__file__)))
    for c in candidates:
        try:
            if os.path.isfile(os.path.join(c, 'item_prices.csv')) or os.path.isfile(os.path.join(c, 'v233_items.json')):
                return c
        except Exception:
            pass
    return os.path.dirname(os.path.abspath(__file__))

def _build_item_table(csv_path):
    ITEMS = {}
    with open(csv_path, 'r', encoding='utf-8') as f:
        rd = csv.DictReader(f)
        for row in rd:
            cid = row['item_id'].strip()
            name = row['name'].strip()
            try:
                val = int(float(row['base_value']))
            except Exception:
                val = 0
            try:
                q = int(float(row['quality']))
            except Exception:
                q = 0
            ITEMS[cid] = {'n': name, 'p': val, 'q': q}
    return ITEMS

def _merge_v233(ITEMS, v233_path):
    try:
        with open(v233_path, 'r', encoding='utf-8') as f:
            v233 = json.load(f)
        merged = dict(v233)
        merged.update(ITEMS)   # CSV takes precedence
        return merged
    except (FileNotFoundError, json.JSONDecodeError):
        return ITEMS

def _record_missing(missing, cid, map_id):
    """记录一个未知 CID 的出现（用于"缺失拍品探测器"）。"""
    d = missing.get(cid)
    if d is None:
        d = {'count': 0, 'maps': set()}
        missing[cid] = d
    d['count'] += 1
    d['maps'].add(map_id)

# prop cost table (from standalone ITEM_COST)
ITEM_COST = {"100100":30000,"100101":2500,"100102":6000,"100103":41000,"100104":1200,"100105":2500,"100106":5600,"100107":7100,"100108":51000,"100110":2500,"100111":2500,"100112":2500,"100113":4400,"100114":61000,"100115":8500,"100116":1200,"100117":2500,"100118":5500,"100119":23000,"100120":68000,"100121":480000,"100122":1200,"100123":2500,"100124":5300,"100125":23000,"100126":300000,"100127":700000,"100128":1200,"100129":2500,"100130":33000,"100131":66000,"100132":130000,"100133":220000,"100134":170000,"100135":1200,"100136":2500,"100137":4300,"100138":15000,"100139":31000,"100140":83000,"100151":4500,"100152":4800,"100153":5100,"100154":4800,"100155":4800,"100156":4700,"100157":4800,"100158":5500,"100159":4000,"100160":4900,"100174":120000,"100161":14000,"100162":12000,"100163":35000,"100164":49000,"100165":4100,"100166":12000,"100167":12000,"100168":19000,"100169":31000,"100170":30000,"100171":30000,"100172":29000,"100173":34000}

# map name table (from standalone MAPS, incl 竞拍之巅)
MAPS = {101:"",102:"",103:"",104:"",105:"",106:"",201:"",304:"",305:"",
2101:"未知快递",2102:"医院快递",2103:"超市快递",2104:"学区快递",2105:"小区快递",2106:"玩具店快递",2107:"古董街快递",
2201:"未知仓库",2202:"潮牌仓库",2203:"硬核资产仓库",2204:"民生储备仓库",2205:"书店仓库",
2301:"杂货集装箱",2302:"家居集装箱",2303:"数码科技集装箱",2304:"冷链集装箱",2305:"古董工艺集装箱",2306:"文博集装箱",2307:"奢华集装箱",2308:"医疗用品集装箱",2309:"军用物资集装箱",2310:"潮牌集装箱",
2401:"未知别墅",2402:"设计师居所",2403:"科学家居所",2404:"养生学家居所",2405:"望族居所",2406:"学者居所",2407:"私人金库",2408:"奢华养老院",2409:"末日庇护所",2410:"极客改造屋",
2501:"未知残骸",2502:"远洋客轮舱房",2503:"军用舰艇保险库",2504:"冷链货船隔离舱",2505:"殖民商船宝库",2506:"探险家座舰资料库",2507:"皇家御用货舱",2508:"生物实验室样本库",2509:"私掠船军火舱",2510:"现代货轮娱乐库",
2601:"隐秘拍卖会",
3101:"未知快递",3102:"医院快递",3103:"超市快递",3104:"学区快递",3105:"小区快递",3106:"玩具店快递",3107:"古董街快递",
3201:"未知仓库",3202:"潮牌仓库",3203:"硬核资产仓库",3204:"民生储备仓库",3205:"书店仓库",
3301:"杂货集装箱",3302:"家居集装箱",3303:"数码科技集装箱",3304:"冷链集装箱",3305:"古董工艺集装箱",3306:"文博集装箱",3307:"奢华集装箱",3308:"医疗用品集装箱",3309:"军用物资集装箱",3310:"潮牌集装箱",
3401:"未知别墅",3402:"设计师居所",3403:"科学家居所",3404:"养生学家居所",3405:"望族居所",3406:"学者居所",3407:"私人金库",3408:"奢华养老院",3409:"末日庇护所",3410:"极客改造屋",
3501:"未知残骸",3502:"远洋客轮舱房",3503:"军用舰艇保险库",3504:"冷链货船隔离舱",3505:"殖民商船宝库",3506:"探险家座舰资料库",3507:"皇家御用货舱",3508:"生物实验室样本库",3509:"私掠船军火舱",3510:"现代货轮娱乐库",
4401:"未知别墅",4402:"设计师居所",4403:"科学家居所",4404:"养生学家居所",4405:"望族居所",4406:"学者居所",4407:"私人金库",4408:"奢华养老院",4409:"末日庇护所",4410:"极客改造屋",
4501:"未知残骸",4502:"远洋客轮舱房",4503:"军用舰艇保险库",4504:"冷链货船隔离舱",4505:"殖民商船宝库",4506:"探险家座舰资料库",4507:"皇家御用货舱",4508:"生物实验室样本库",4509:"私掠船军火舱",4510:"现代货轮娱乐库",
4511:"未知残骸",4512:"远洋客轮舱房",4513:"军用舰艇保险库",4514:"冷链货船隔离舱",4515:"殖民商船宝库",4516:"探险家座舰资料库",4517:"皇家御用货舱",4518:"生物实验室样本库",4519:"私掠船军火舱",4520:"现代货轮娱乐库",
5601:"百物杂拍",5602:"居家陈设",5603:"秘药器械",5604:"奢品风尚",5605:"军械私拍",5606:"珍宝原石",5607:"古物珍玩",5608:"尖端数码",5609:"载具机械",5610:"珍馐食材",5611:"翰墨典藏"}

def get_ticket(mid):
    p = mid // 100
    if p == 21 or p == 31: return 0        # 快递
    if p == 22 or p == 32: return 2000     # 仓库
    if p == 23 or p == 33: return 5000     # 集装箱
    if p == 24 or p == 34 or p == 44: return 10000  # 别墅
    if p == 25 or p == 35 or p == 45: return 25000  # 殘骸
    if p == 26: return 100000              # 拍卖会
    if p == 56: return 15000               # 竞拍之巅
    return 0

def map_name(mid):
    return MAPS.get(mid) or ('地图' + str(mid))

def fmt_time(ts):
    try:
        dt = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc) + datetime.timedelta(hours=8)
        return dt.strftime('%m/%d %H:%M')
    except Exception:
        return '--'

def last_bid(plist):
    """Final bid = last PriceLog entry's ItemCidOrPrice (by Round). Works for both S2C_89 and S2C45."""
    bids = []
    for p in (plist or []):
        r = p.get('Round') if isinstance(p.get('Round'), int) else 0
        bids.append((r, p.get('ItemCidOrPrice', 0)))
    bids.sort()
    return bids[-1][1] if bids else 0

def build_items_used(use_log, ITEMS):
    items_used = []
    item_cost = 0
    for it in (use_log or []):
        c = str(it.get('ItemCidOrPrice', ''))
        r = it.get('Round')
        r = (r + 1) if isinstance(r, int) else 1
        cost = ITEM_COST.get(c, 0)
        item_cost += cost
        items_used.append({'round': r, 'cid': c, 'name': ITEMS.get(c, {}).get('n', '?'), 'cost': cost})
    items_used.sort(key=lambda x: x['round'])
    return items_used, item_cost

def extract_inventory(log, items_table=None):
    """扫描日志提取『当前道具库存』（技能道具及对局内可用道具）。
    数据源：
    - BuildItems 行：登录/进仓库/开局时全量打印所有道具 + count（快照底数），含 quality(档位)/count(数量) 字段。
    - S2C_19_add_item_notify：买/用/获得道具的实时增量（OldCount→NewCount 绝对量）。
    两路按日志先后顺序合并（后出现覆盖先出现），得到最新持有量。
    返回 dict: cid -> {'name': 名称, 'count': 数量, 'quality': 档位}。"""
    import json as _json
    out = {}
    try:
        with open(log, 'rb') as fp:
            data = fp.read()
    except Exception:
        return out

    # --- 1) BuildItems 快照（itemType=道具大类型，quality=档位，count=数量）---
    build_re = re.compile(
        rb'BuildItems :\(item:([^\s]+) (\d{6}) (\d+),'
        rb'size:[^,]+,qualityCID:\d+,No:\d+,quality:(\d+),'
        rb'itemType:(\d+),pos:\d+,rotate:(?:True|False),'
        rb'stockId:\d+,count:(\d+),canSale:(?:True|False)\)'
    )
    snap_events = []  # (offset, name, cid, quality, count)
    for m in build_re.finditer(data):
        snap_events.append((
            m.start(),
            m.group(1).decode('utf-8', 'replace'),
            m.group(2).decode(),
            int(m.group(4)),
            int(m.group(6)),
        ))

    # --- 2) S2C_19 实时增量（CID -> NewCount 绝对量）---
    inc_events = []  # (offset, itemlist)
    marker = b'(S2C_19_add_item_notify)'
    p = data.find(marker)
    while p != -1:
        start = data.find(b'{', p)
        depth = 0; i = start; n = len(data)
        while i < n:
            c = data[i]
            if c == 123: depth += 1
            elif c == 125:
                depth -= 1
                if depth == 0: break
            i += 1
        try:
            d = _json.loads(data[start:i + 1])
            inc_events.append((p, d.get('ItemList', [])))
        except Exception:
            pass
        p = data.find(marker, i)

    # --- 3) 合并 ---
    # 关键：S2C_19 是服务端推给【当前账号】的实时绝对量（OldCount→NewCount），
    # 必须作为权威值；BuildItems 是背包快照，可能来自其他账号或已过时，
    # 故仅作为『没有任何增量事件时』的兜底底数（先填，再被 S2C_19 覆盖）。
    for off, name, cid, quality, count in snap_events:
        out[cid] = {'name': name, 'count': count, 'quality': quality}
    for off, itemlist in inc_events:
        for it in itemlist:
            cid = str(it.get('ItemCid', ''))
            if not cid or cid in ('1', '2'):
                continue  # 跳过银币/金币等货币
            nc = it.get('NewCount')
            if nc is None:
                continue
            if cid in out:
                out[cid]['count'] = int(nc)
            else:
                out[cid] = {'name': '', 'count': int(nc), 'quality': 0}

    # 名称/档位兜底：用基础物品表补全 S2C_19 独有项
    if items_table:
        for cid, info in out.items():
            if not info['name'] and cid in items_table:
                info['name'] = items_table[cid].get('n', '')
            if not info['quality'] and cid in items_table:
                info['quality'] = items_table[cid].get('q', 0)
    return out

def emit(map_id, win_uid, ts, rounds, is_win, final_bid, w_final, w_name,
        items_used, item_cost, container_val, unpriced, won_items,
        opponent_bid, opponent_uid, opponent_name):
    ticket = get_ticket(map_id)
    my_base = (container_val - final_bid - ticket - item_cost) if is_win else (-ticket - item_cost)
    w_base = container_val - w_final - ticket
    dividend = int(abs(w_base) * 0.1) if w_base <= -10000 else 0
    my_profit = my_base + dividend
    disp_profit = container_val - w_final - ticket
    return {
        'time': fmt_time(ts), 'ts': ts, 'map_id': map_id,
        'map_name': map_name(map_id), 'rounds': rounds, 'is_win': is_win,
        'winner_name': '自己' if is_win else w_name,
        'items_used': items_used, 'won_items': won_items,
        'unpriced_item_count': unpriced,
        'final_bid': final_bid, 'winner_final_bid': w_final,
        'opponent_bid': opponent_bid, 'opponent_bidder_uid': opponent_uid,
        'opponent_bidder_name': opponent_name,
        'actual_value': container_val, 'disp_profit': disp_profit,
        'my_profit': my_profit, 'ticket': ticket, 'item_cost': item_cost
    }

def extract_silver_samples(log, uid=None, skip_history=False):
    """扫描日志提取银币(SilverCoin)采样，返回 [{ts,time,value}] 列表（按 ts 升序）。
    对齐 v2.34：日志未记录(被 DLL 省略或字段缺失)时返回空列表，绝不推算假余额。
    银币在游戏日志里是带引号的字符串 "5571654"，正则兼容可选引号。

    性能优化(2026-08-03)：引入缓存文件 silver_cache.json（与日志同目录），
    记录每个已扫文件的 mtime；下次只重扫「新增/改动」的文件、跳过未变的，
    避免每次解析都全扫数百 MB 历史会话存档导致云电脑卡死。
    - skip_history=False：增量扫描全部历史存档（首次/启动建缓存用）
    - skip_history=True ：历史部分直接复用缓存，仅重扫当前日志（实时刷新用）
    Unity 日志行无时间戳：live log 用扫描时刻；历史存档用文件名里的时间。"""
    import json as _json
    # 仅采集“余额快照”类行：跳过交易金额事件（如 S2C_443_claim_exchange_sell_income
    # “领取兑换出售收益”，其 SilverCoin 是单笔收入金额而非钱包余额，会把当前银币带偏）。
    pat = re.compile(r'"SilverCoin"\s*:\s*"?(\d+)"?')
    bad_evt = re.compile(r'ClaimExchange|SellIncome|claim_exchange|sell_income|ExchangeSell|C2S442|S2C_443|_443')
    _CACHE_VER = 2
    cache_path = os.path.join(os.path.dirname(os.path.abspath(log)), "silver_cache.json")
    cache = {"version": _CACHE_VER, "files": {}, "live_mtime": 0.0, "live_samples": []}
    try:
        if os.path.exists(cache_path):
            with open(cache_path, "r", encoding="utf-8") as f:
                loaded = _json.load(f)
            # 缓存版本不一致时整体作废重扫（旧缓存可能带“交易金额”脏采样）
            if isinstance(loaded, dict) and loaded.get("version") == _CACHE_VER:
                cache["files"] = loaded.get("files", {}) or {}
                cache["live_mtime"] = float(loaded.get("live_mtime", 0.0) or 0.0)
                cache["live_samples"] = loaded.get("live_samples", []) or []
    except Exception:
        cache = {"version": _CACHE_VER, "files": {}, "live_mtime": 0.0, "live_samples": []}

    def scan_file(path, ts, stamp):
        out = []
        try:
            with open(path, 'r', encoding='utf-8', errors='replace') as f:
                for line in f:
                    if bad_evt.search(line):
                        continue  # 交易金额事件，非余额，跳过
                    m = pat.search(line)
                    if m:
                        try:
                            out.append({'ts': ts, 'time': stamp, 'value': int(m.group(1))})
                        except ValueError:
                            pass
        except Exception:
            pass
        return out

    # 1) 当前日志（live）：mtime 未变则复用缓存，否则重扫
    now = datetime.datetime.now()
    try:
        live_mtime = os.path.getmtime(log)
    except Exception:
        live_mtime = 0.0
    if cache["live_mtime"] == live_mtime and cache["live_samples"] is not None:
        live_samples = cache["live_samples"]
    else:
        live_samples = scan_file(log, now.timestamp(), now.strftime('%Y-%m-%d %H:%M:%S'))
        cache["live_mtime"] = live_mtime
        cache["live_samples"] = live_samples

    # 2) 历史会话存档
    files_cache = cache["files"]
    if skip_history and files_cache:
        # 刷新模式：历史直接复用缓存，不碰磁盘历史文件
        hist_samples = []
        for rec in files_cache.values():
            hist_samples.extend(rec.get("samples", []))
    else:
        hist_samples = []
        if uid:
            d = os.path.dirname(os.path.abspath(log))
            prefix = f"{uid}_"
            seen = set()
            for fn in os.listdir(d):
                if not fn.startswith(prefix) or not fn.endswith('.txt'):
                    continue
                fp = os.path.join(d, fn)
                try:
                    fmtime = os.path.getmtime(fp)
                except Exception:
                    continue
                seen.add(fn)
                rec = files_cache.get(fn)
                if rec is not None and rec.get("mtime") == fmtime and rec.get("samples") is not None:
                    hist_samples.extend(rec["samples"])  # 未变，复用缓存
                    continue
                ts_part = fn[len(prefix):-4].strip()
                fts = None
                try:
                    fts = datetime.datetime.strptime(ts_part, '%Y-%m-%d %H-%M-%S')
                except Exception:
                    try:
                        fts = datetime.datetime.fromtimestamp(fmtime)
                    except Exception:
                        fts = None
                if fts is None:
                    continue
                s = scan_file(fp, fts.timestamp(), fts.strftime('%Y-%m-%d %H:%M:%S'))
                files_cache[fn] = {"mtime": fmtime, "samples": s}
                hist_samples.extend(s)
            # 清理已删除文件对应的缓存
            for fn in list(files_cache.keys()):
                if fn not in seen:
                    files_cache.pop(fn, None)

    # 合并排序并写回缓存
    samples = live_samples + hist_samples
    samples.sort(key=lambda s: s['ts'])
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            _json.dump({"version": _CACHE_VER, "files": files_cache,
                        "live_mtime": cache["live_mtime"],
                        "live_samples": live_samples}, f)
    except Exception:
        pass
    return samples


def parse(log, uid='auto', csv='item_prices.csv', out='result.json', verbose=True, on_game=None, db_path=None, skip_history=False):
    """解析 Player.log，返回 result 字典并写入 out。可被 serve.py / exe 直接调用。"""
    HERE = os.path.dirname(os.path.abspath(__file__))
    ITEMS = _build_item_table(csv)
    ITEMS = _merge_v233(ITEMS, os.path.join(RES_DIR or HERE, 'v233_items.json'))

    # 从 SQLite DB 读取自定义价格/名称，覆盖 ITEMS 中的 p/n；
    # 若 CID 在基础表里不存在（日志里出现但项目未收录的新物品），则直接加入 ITEMS。
    try:
        import sqlite3
        db_path_resolved = db_path or os.path.join(_root_dir(), 'item_prices.db')
        conn = sqlite3.connect(db_path_resolved)
        cur = conn.cursor()
        # 兼容旧库（仅有 cid/price，无 name 列）
        try:
            cur.execute("SELECT cid, price, name FROM item_prices")
            rows = [(r[0], r[1], r[2]) for r in cur.fetchall()]
        except Exception:
            cur.execute("SELECT cid, price FROM item_prices")
            rows = [(r[0], r[1], None) for r in cur.fetchall()]
        conn.close()
        for cid, price, name in rows:
            if cid in ITEMS:
                if price and price > 0:
                    ITEMS[cid]['p'] = price
                if name:
                    ITEMS[cid]['n'] = name
            else:
                ITEMS[cid] = {
                    'n': name or ('未知物品#' + str(cid)),
                    'p': price if (price and price > 0) else 0,
                    'q': 0,
                }
    except Exception:
        pass  # DB 不存在或读取失败不影响默认行为

    # 缺失拍品收集：记录日志里出现、但 ITEMS 中查不到的 CID（含出现次数与地图）
    missing = {}

    # ---- read log (mmap 内存映射：文件留在磁盘、按需分页，不整文件读入内存，
    #        根治大日志(数百 MB)在内存小的云电脑上 OOM 被杀、退出码 -1 的问题) ----
    _log_fp = open(log, 'rb')
    try:
        text = mmap.mmap(_log_fp.fileno(), 0, access=mmap.ACCESS_READ)
    except ValueError:
        text = _log_fp.read()  # 空文件无法 mmap，退回读取 bytes(同样走下面的 bytes 逻辑)

    # ============ SOURCE 1: S2C_89 (history GameLogList) ============
    # 读取所有 S2C_89 块并合并 GameLogList（后面的块可能包含更多历史数据）
    marker89 = b'(S2C_89_get_game_log_list)'
    games89_raw = []
    seen89_uids = set()
    p89 = text.find(marker89)
    while p89 != -1:
        start = text.find(b'{', p89)
        depth = 0; i = start; n = len(text)
        while i < n:
            c = text[i]
            if c == 123: depth += 1
            elif c == 125:
                depth -= 1
                if depth == 0: break
            i += 1
        try:
            data = json.loads(text[start:i+1])
            for g in data.get('GameLogList', []):
                # 按 Uid 去重：同一个 GameUid 只取一次（后出现的更完整）
                guid = g.get('Uid', '')
                if guid and guid not in seen89_uids:
                    seen89_uids.add(guid)
                    games89_raw.append(g)
                elif not guid:
                    games89_raw.append(g)
        except (json.JSONDecodeError, Exception):
            pass
        p89 = text.find(marker89, i)
    games89 = games89_raw
    if verbose:
        print('Found', len(games89), 'games in S2C_89')

    # ============ SOURCE 2: S2C45 (real-time game-over notify) ============
    marker45 = b'(S2C_45_game_over_notify){'
    s45 = []
    p = text.find(marker45)
    while p != -1:
        start = text.find(b'{', p)
        depth = 0; i = start; n = len(text)
        while i < n:
            c = text[i]
            if c == 123: depth += 1
            elif c == 125:
                depth -= 1
                if depth == 0: break
            i += 1
        try:
            d = json.loads(text[start:i+1])
            s45.append(d)
        except Exception:
            pass
        p = text.find(marker45, i)
    if verbose:
        print('Found', len(s45), 'S2C45 game-over messages')

    # 释放日志文件 / mmap（不再需要整段 text）
    try:
        text.close()
    except Exception:
        pass
    try:
        _log_fp.close()
    except Exception:
        pass

    # ============ auto-detect UID (most frequent player across both) ============
    if not uid or uid == 'auto':
        freq = {}
        for g in games89:
            for u in (g.get('LogGameUserList') or []):
                uu = u.get('UserUid')
                if uu:
                    freq[uu] = freq.get(uu, 0) + 1
        for d in s45:
            for u in (d.get('GameData', {}).get('UserLog') or []):
                uu = u.get('UserUid')
                if uu:
                    freq[uu] = freq.get(uu, 0) + 1
        if freq:
            uid = max(freq, key=freq.get)
            if verbose:
                print('Auto-detected UID:', uid, '(', freq[uid], 'appearances )')
        else:
            if verbose:
                print('Could not auto-detect UID; parsing all games without owner filter')

    # ============ parse S2C_89 -> records ============
    records = []
    for g in games89:
        map_id = g.get('MapId')
        win_uid = g.get('WinUserUid', '')
        is_win = (win_uid == uid)
        try:
            ts = int(g.get('GameOverTime', 0))
        except Exception:
            ts = 0
        users = g.get('LogGameUserList', [])
        my = next((u for u in users if u.get('UserUid') == uid), None)
        if my is None:
            continue
        rnd = g.get('Round', None)
        rounds = (rnd + 1) if isinstance(rnd, int) else (max([pl.get('Round', 0) for pl in (my.get('PriceLog') or [])] + [0]) + 2)
        # S2C_89 carries the authoritative final price in LastPrice (NOT the PriceLog tail)
        final_bid = my.get('LastPrice', 0)
        items_used, item_cost = build_items_used(my.get('UseItemLog'), ITEMS)
        stock = g.get('StockItemList', []) or []
        container_val = 0; unpriced = 0; won_items = []; missing_cids = []
        for cid in stock:
            cs = str(cid)
            info = ITEMS.get(cs)
            if info:
                container_val += info['p']
                won_items.append({'uid': 'item-' + cs, 'cid': cs, 'name': info['n'], 'value': info['p']})
            else:
                unpriced += 1
                _record_missing(missing, cs, map_id)
                missing_cids.append(cs)
        w = next((u for u in users if u.get('UserUid') == win_uid), None)
        w_final = (w.get('LastPrice', 0) if w else 0)
        w_name = w.get('Name', '?') if w else '?'
        if is_win:
            losers = [u for u in users if u.get('UserUid') != uid]
            top = max(losers, key=lambda u: u.get('LastPrice', 0)) if losers else None
            opponent_bid = (top.get('LastPrice', 0) if top else 0)
            opponent_uid = top.get('UserUid', '') if top else ''
            opponent_name = top.get('Name', '') if top else ''
        else:
            opponent_bid = w_final
            opponent_uid = win_uid
            opponent_name = w_name
        rec = emit(map_id, win_uid, ts, rounds, is_win, final_bid, w_final, w_name,
                  items_used, item_cost, container_val, unpriced, won_items,
                  opponent_bid, opponent_uid, opponent_name)
        rec['missing_cids'] = missing_cids
        records.append(rec)
        if on_game:
            try:
                on_game(rec, uid)
            except Exception:
                pass

    # ============ parse S2C45 -> records (dedupe against S2C_89) ============
    seen = set()
    for rec in records:
        seen.add((rec['map_id'], rec['winner_final_bid'] if not rec['is_win'] else rec['final_bid'], rec['ts'] // 60, rec['winner_name'] if not rec['is_win'] else 'self'))

    for d in s45:
        gd = d.get('GameData', {})
        win_uid = d.get('WinUserUid', '')
        map_id = gd.get('MapId')
        try:
            ts = int(gd.get('ServerTime', 0))
        except Exception:
            ts = 0
        is_win = (win_uid == uid)
        users = gd.get('UserLog', [])
        my = next((u for u in users if u.get('UserUid') == uid), None)
        if my is None:
            continue
        rnd = gd.get('Round', None)
        rounds = (rnd + 1) if isinstance(rnd, int) else (max([pl.get('Round', 0) for pl in (my.get('PriceLog') or [])] + [0]) + 2)
        final_bid = last_bid(my.get('PriceLog'))
        items_used, item_cost = build_items_used(my.get('UseItemLog'), ITEMS)
        container_val = 0; unpriced = 0; won_items = []; uids = set(); missing_cids = []
        for box in (gd.get('StockContainer', {}).get('StockBoxes') or []):
            item = box.get('Item') or {}
            cid = item.get('Cid')
            if not cid:
                continue
            iu = item.get('Uid')
            if iu and iu in uids:
                continue
            if iu:
                uids.add(iu)
            if item.get('CanTrade') is False:
                continue
            cs = str(cid)
            info = ITEMS.get(cs)
            if info:
                container_val += info['p']
                won_items.append({'uid': 'item-' + cs, 'cid': cs, 'name': info['n'], 'value': info['p']})
            else:
                unpriced += 1
                _record_missing(missing, cs, map_id)
                missing_cids.append(cs)
        w = next((u for u in users if u.get('UserUid') == win_uid), None)
        w_final = last_bid(w.get('PriceLog')) if w else 0
        w_name = w.get('Name', '?') if w else '?'
        if is_win:
            losers = [u for u in users if u.get('UserUid') != uid]
            top = max(losers, key=lambda u: last_bid(u.get('PriceLog'))) if losers else None
            opponent_bid = last_bid(top.get('PriceLog')) if top else 0
            opponent_uid = top.get('UserUid', '') if top else ''
            opponent_name = top.get('Name', '') if top else ''
        else:
            opponent_bid = w_final
            opponent_uid = win_uid
            opponent_name = w_name
        key = (map_id, w_final if not is_win else final_bid, ts // 60, w_name if not is_win else 'self')
        if key in seen:
            continue
        seen.add(key)
        rec = emit(map_id, win_uid, ts, rounds, is_win, final_bid, w_final, w_name,
                  items_used, item_cost, container_val, unpriced, won_items,
                  opponent_bid, opponent_uid, opponent_name)
        rec['missing_cids'] = missing_cids
        records.append(rec)
        if on_game:
            try:
                on_game(rec, uid)
            except Exception:
                pass

    records.sort(key=lambda x: x['ts'], reverse=True)
    if verbose:
        print('Parsed', len(records), 'total games (S2C_89 + S2C45 merged) for uid', uid)

    # ---- formula self-consistency check ----
    ok = True
    for g in records:
        if g['disp_profit'] != g['actual_value'] - g['winner_final_bid'] - g['ticket']:
            ok = False
            print('DISP_PROFIT MISMATCH map', g['map_id'], g['disp_profit'], '!=', g['actual_value'] - g['winner_final_bid'] - g['ticket'])
        w_base = g['actual_value'] - g['winner_final_bid'] - g['ticket']
        dividend = int(abs(w_base) * 0.1) if w_base <= -10000 else 0
        expected_my = (g['actual_value'] - g['final_bid'] - g['ticket'] - g['item_cost']) if g['is_win'] else (-g['ticket'] - g['item_cost'])
        expected_my += dividend
        if g['my_profit'] != expected_my:
            ok = False
            print('MY_PROFIT MISMATCH map', g['map_id'], g['my_profit'], '!=', expected_my)
    if unpriced_total := sum(g['unpriced_item_count'] for g in records):
        print('NOTE: %d items had no price entry (shown as value 0)' % unpriced_total)
    if verbose:
        print('VERIFY:', 'ALL MATCH' if ok else 'MISMATCH FOUND')

    wins = sum(1 for g in records if g['is_win'])
    losses = len(records) - wins
    # 盈利拿仓数：赢且盈亏 > 0 的局数
    profitable_wins = sum(1 for g in records if g['is_win'] and g['my_profit'] > 0)
    dividend_total = 0
    for g in records:
        wb = g['actual_value'] - g['winner_final_bid'] - g['ticket']
        if wb <= -10000:
            dividend_total += int(abs(wb) * 0.1)
    summary = {
        'games': len(records), 'wins': wins, 'losses': losses,
        'win_rate': (wins / len(records) * 100) if records else 0,
        'profit_rate': (profitable_wins / wins * 100) if wins else 0,
        'profit': sum(g['my_profit'] for g in records),
        'value': sum(g['actual_value'] for g in records if g['is_win']),
        'bid': sum(g['final_bid'] for g in records if g['is_win']),
        'item_cost': sum(g['item_cost'] for g in records),
        'unpriced_item_count': sum(g['unpriced_item_count'] for g in records),
        'ticket': sum(g['ticket'] for g in records),
        'dividend': dividend_total
    }
    if verbose:
        print('SUMMARY:', json.dumps(summary, ensure_ascii=False))

    # 缺失拍品：转换 set→list 以便 JSON 序列化
    missing_items = [
        {'cid': cid, 'count': d['count'], 'maps': sorted(d['maps'])}
        for cid, d in missing.items()
    ]
    result = {'meta': {
        'uid': uid,
        'parser_version': '2.34-fixed',
        'source_files': [log, csv],
        'generated_at': datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    }, 'summary': summary, 'games': records, 'missing_items': missing_items}
    # 道具当前库存：扫描日志 BuildItems(快照) + S2C_19(实时增量) 合并得到最新持有量
    try:
        result['inventory'] = extract_inventory(log, ITEMS)
    except Exception:
        result['inventory'] = {}
    # 银币走势：扫描日志提取 SilverCoin 采样（日志未记录则为空列表，绝不推算假余额）
    try:
        result['silver_samples'] = extract_silver_samples(log, uid, skip_history=skip_history)
    except Exception:
        result['silver_samples'] = []
    # 原子写：先写临时文件并落盘，再 os.replace 整体替换，避免关机/崩溃时留下半截或损坏的 result.json
    # out=None 时跳过写文件（探测器模式只取 missing_items，不落盘）
    if out:
        tmp = out + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, out)
    if verbose:
        print('Wrote', out)
    return result

def detect_missing_items(log, uid='auto', db_path=None):
    """扫描日志，返回所有"出现但未被物品表收录"的 CID 及其出现次数/地图。
    直接复用 parse()（out=None 不落盘、on_game=None 不写库），保证与正式解析口径一致。"""
    try:
        result = parse(log, uid=uid, out=None, on_game=None, verbose=False, db_path=db_path)
        return result.get('missing_items', [])
    except Exception:
        return []

def get_base_item_cids(csv='item_prices.csv'):
    """返回基础物品表（csv + v233）收录的所有 CID 集合，用于区分"用户自添加的新物品"。"""
    HERE = os.path.dirname(os.path.abspath(__file__))
    ITEMS = _build_item_table(os.path.join(HERE, csv))
    ITEMS = _merge_v233(ITEMS, os.path.join(RES_DIR or HERE, 'v233_items.json'))
    return set(ITEMS.keys())

if __name__ == '__main__':
    LOG = sys.argv[1] if len(sys.argv) > 1 else 'Player.log'
    UID = sys.argv[2] if len(sys.argv) > 2 else 'auto'
    CSV = sys.argv[3] if len(sys.argv) > 3 else 'item_prices.csv'
    OUT = sys.argv[4] if len(sys.argv) > 4 else 'result.json'
    parse(LOG, UID, CSV, OUT)
