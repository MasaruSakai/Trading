#!/usr/bin/env python3
"""
戦略バックテスト（1年）: 各ウォッチリストの「良い1銘柄をキープ」
======================================================================
ルール（ユーザー指定）:
  - 対象: 米ハイテク / 米国銘柄 / 米国セクター（各ウォッチリスト独立に1ポジション）
  - 選択: 条件合格銘柄のうち「売買代金」最大の1銘柄
  - 保有: シグナルが続く限り保有。保有銘柄が条件を外れたら翌寄りで入替(or 現金)
  - 売買: 翌寄り(open-to-open)で執行。手数料・スリッページは無視
  - 比較: QQQ 買い持ち

シグナル(近似):
  capital_distribution は履歴が無いため、capital_flow(日次)の
    super_in_flow / big_in_flow（ティア別の日次純流入）を代理に使う。
  週次大口は「日次big_in_flowの直近5営業日ローリング合計」で代理。
  合格条件: super>0 かつ big>0 かつ week_big>0 （ライブと同じ構造）

注意（バイアス）:
  - 近似シグナル（ライブの当日intraday distributionとは別物）
  - ウォッチリストは現在の構成のみ取得可 → 生存者バイアスで強めに出やすい
  - 手数料・スリッページ・約定不可リスクは未考慮

出力: 標準出力サマリ + logs/backtest_strategy_equity.csv（日次エクイティ）
"""
import sys, time, argparse, os
from datetime import datetime, timedelta

sys.path.insert(0, '/Users/masaru/.claude/skills/moomooapi/scripts')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from moomoo import OpenQuoteContext, RET_OK, PeriodType

OPEND_HOST, OPEND_PORT = '127.0.0.1', 11111
WATCHLISTS = ['米ハイテク', '米国銘柄', '米国セクター']
CALL_INTERVAL = 1.05
WEEK_WINDOW = 5         # 週次大口の代理（直近N営業日のbig合計）
LOG_DIR = '/Users/masaru/Projects/Trading/logs'


def fetch_code_data(ctx, code, start, end):
    """日足(open/close/turnover) と 日次フロー(super/big) を date で結合して返す。"""
    days = {}
    rk, k, _ = ctx.request_history_kline(code, start=start, end=end,
                                         ktype='K_DAY', max_count=400)
    time.sleep(CALL_INTERVAL)
    if rk != RET_OK or k.empty:
        return None
    for _, r in k.iterrows():
        d = str(r['time_key'])[:10]
        days[d] = {'open': float(r['open'] or 0), 'close': float(r['close'] or 0),
                   'turnover': float(r['turnover'] or 0), 'super': None, 'big': None}
    rf, f = ctx.get_capital_flow(code, period_type=PeriodType.DAY)
    time.sleep(CALL_INTERVAL)
    if rf == RET_OK and not f.empty:
        for _, r in f.iterrows():
            d = str(r['capital_flow_item_time'])[:10]
            if d in days:
                days[d]['super'] = float(r.get('super_in_flow', 0) or 0)
                days[d]['big'] = float(r.get('big_in_flow', 0) or 0)
    # 週次大口の代理: big の直近5営業日ローリング合計
    sd = sorted(days)
    supers, bigs = [], []
    for d in sd:
        s = days[d]['super'] if days[d]['super'] is not None else 0.0
        b = days[d]['big'] if days[d]['big'] is not None else 0.0
        supers.append(s); bigs.append(b)
        days[d]['week_big'] = sum(bigs[-WEEK_WINDOW:])
        window_sb = [sv + bv for sv, bv in zip(supers[-WEEK_WINDOW:], bigs[-WEEK_WINDOW:])]
        import statistics as _st
        days[d]['flow_median'] = _st.median(window_sb) if window_sb else 0.0
        days[d]['pos_days'] = sum(1 for v in window_sb if v > 0)
        days[d]['sell_days'] = sum(1 for v in window_sb if v <= 0)
        days[d]['window_len'] = len(window_sb)
        wl = len(window_sb)
        sd_ = days[d]['sell_days']
        days[d]['sell_median'] = days[d]['flow_median'] <= 0
        days[d]['sell_strict'] = sd_ >= wl if wl < WEEK_WINDOW else sd_ >= wl - 1
    return days


# レバレッジ/逆張り・暗号ETFの名前キーワード（コモディティは残す）
LEV_KW = ['bull', 'bear', 'ultra', '2x', '3x', '1.5x', 'leveraged', 'inverse', 'short']
CRYPTO_KW = ['bitcoin', 'ethereum', 'crypto', 'blockchain', ' btc', ' eth']


def build_excluded(etf_names, codes):
    """レバ/逆張り・暗号ETFのコード集合を返す（名前で判定。コモディティは除外しない）。"""
    ex = set()
    for c in codes:
        nm = (etf_names.get(c) or '').lower()
        if not nm:
            continue
        if any(k in nm for k in LEV_KW) or any(k in nm for k in CRYPTO_KW):
            ex.add(c)
    return ex


def qualifies(rec):
    return (rec.get('super') is not None and rec.get('big') is not None
            and rec['super'] > 0 and rec['big'] > 0 and rec.get('week_big', 0) > 0)


def qualifies_standard(rec):
    """標準版: Filter①=super>0, Filter②=5日中4日以上(super+big)>0。"""
    if rec.get('super') is None or rec.get('big') is None:
        return False
    if rec['super'] <= 0:
        return False
    wl = rec.get('window_len', 0)
    pd = rec.get('pos_days', 0)
    if wl < WEEK_WINDOW:
        return pd >= wl
    return pd >= wl - 1  # 4/5日以上


def qualifies_enhanced(rec):
    """改善版: Filter①=super>0, Filter②=median(super+big)>0。"""
    if rec.get('super') is None or rec.get('big') is None:
        return False
    if rec['super'] <= 0:
        return False
    return rec.get('flow_median', 0) > 0


def simulate(group_codes, data, calendar):
    """1ウォッチリストの単一ポジション・シグナル継続保有を再現（先読みなし）。
    シグナルは day i の終値時点で確定 → 翌日 open(i+1) で建玉 →
    区間 open(i+1)→open(i+2) のリターンを得る。
    戻り値: 日次リターン列(長さ len(calendar)-1。index i は区間[i+1,i+2]に対応),
            取引回数, 保有日数。"""
    rets = [0.0] * (len(calendar) - 1)
    held = None
    trades, held_days = 0, 0
    for i in range(len(calendar) - 2):
        di = calendar[i]
        # シグナル日 di（終値時点で確定）での合格者
        quals = [c for c in group_codes if c in data and di in data[c] and qualifies(data[c][di])]
        # 保有継続判定（保有銘柄がまだシグナル継続ならキープ）
        if held is not None and held in quals:
            pass
        else:
            new = max(quals, key=lambda c: data[c][di]['turnover']) if quals else None
            if new != held and new is not None:
                trades += 1
            held = new
        # 建玉は翌日 open(i+1)、リターンは open(i+1)→open(i+2)（先読み回避）
        if held is not None:
            a = data[held].get(calendar[i + 1])
            b = data[held].get(calendar[i + 2])
            if a and b and a['open'] > 0 and b['open'] > 0:
                rets[i] = b['open'] / a['open'] - 1.0
                held_days += 1
    return rets, trades, held_days


def simulate_portfolio(hi_codes, se_codes, data, cal, bench='US.QQQM',
                       hi_qual=None, se_qual=None):
    """整数株ポートフォリオ。予算=その時のQQQM 1株価格。
    米ハイテク2/3(改善版フィルタ)・米セクター1/3(標準版フィルタ)を整数株で近似。
    合格無しは現金。リターンは予算に対する損益率。
    戻り値: 日次リターン列, ポジション変更回数, 平均投資比率。"""
    if hi_qual is None:
        hi_qual = qualifies_enhanced
    if se_qual is None:
        se_qual = qualifies_standard
    rets = [0.0] * (len(cal) - 1)
    held_h = held_s = None  # 保有中の銘柄
    changes = 0
    inv_fracs = []

    def _is_sell_h(code, di):
        """米ハイテク用エグジット: sell_median が出たら手放す。"""
        rec = data.get(code, {}).get(di)
        return rec is None or rec.get('sell_median', False)

    def _is_sell_s(code, di):
        """米セクター用エグジット: sell_strict が出たら手放す。"""
        rec = data.get(code, {}).get(di)
        return rec is None or rec.get('sell_strict', False)

    def _pick(held, qual_list, budget_alloc, di1, allow_over=False):
        """合格リストを売買代金順に辿り、予算に収まる最初の銘柄を返す。"""
        ordered = ([held] if held and held in [c for c, _ in qual_list] else []) + \
                  [c for c, _ in qual_list if c != held]
        for c in ordered:
            p1 = data[c].get(di1)
            if not p1 or p1['open'] <= 0:
                continue
            sh = round(budget_alloc / p1['open'])
            if sh == 0 and allow_over and p1['open'] <= budget_alloc * 1.15:
                sh = 1
            if sh > 0:
                return c, sh, p1['open']
        return None, 0, 0

    for i in range(len(cal) - 2):
        di = cal[i]
        # 合格リスト（エントリー用・売買代金降順）
        qh = sorted(
            [(c, data[c][di]) for c in hi_codes if c in data and di in data[c] and hi_qual(data[c][di])],
            key=lambda x: x[1]['turnover'], reverse=True)
        qs = sorted(
            [(c, data[c][di]) for c in se_codes if c in data and di in data[c] and se_qual(data[c][di])],
            key=lambda x: x[1]['turnover'], reverse=True)
        qh_codes = [c for c, _ in qh]
        qs_codes = [c for c, _ in qs]

        # 米ハイテク: 売りシグナルが出たら手放す / なければ保持継続 / 空なら新規エントリー
        if held_h is not None and _is_sell_h(held_h, di):
            held_h = None
            changes += 1
        if held_h is None and qh_codes:
            held_h = qh_codes[0]
            changes += 1

        # 米セクター: 売りシグナルが出たら手放す / なければ保持継続 / 空なら新規エントリー
        if held_s is not None and _is_sell_s(held_s, di):
            held_s = None
            changes += 1
        if held_s is None and qs_codes:
            held_s = qs_codes[0]
            changes += 1

        bo = data[bench].get(cal[i + 1])
        if not bo or bo['open'] <= 0:
            continue
        budget = bo['open'] * 10
        pnl = spent = 0.0

        # 米ハイテク 2/3: 高すぎれば次候補へ
        exec_pool_h = ([(held_h, data[held_h][di])] if held_h and held_h in data and di in data[held_h] else []) + \
                      [(c, r) for c, r in qh if c != held_h]
        if exec_pool_h:
            c, sh, price = _pick(held_h, exec_pool_h, budget * 2 / 3, cal[i + 1], allow_over=True)
            if c:
                p2 = data[c].get(cal[i + 2])
                if p2 and p2['open'] > 0:
                    pnl += sh * (p2['open'] - price); spent += sh * price

        # 米セクター 1/3: 残予算内で次候補へ
        exec_pool_s = ([(held_s, data[held_s][di])] if held_s and held_s in data and di in data[held_s] else []) + \
                      [(c, r) for c, r in qs if c != held_s]
        if exec_pool_s:
            rem = max(0.0, budget - spent)
            c, ss, price = _pick(held_s, exec_pool_s, min(budget / 3, rem), cal[i + 1], allow_over=False)
            if c:
                p2 = data[c].get(cal[i + 2])
                if p2 and p2['open'] > 0:
                    pnl += ss * (p2['open'] - price); spent += ss * price

        rets[i] = pnl / budget
        inv_fracs.append(spent / budget)
    return rets, changes, (sum(inv_fracs) / len(inv_fracs) if inv_fracs else 0)


def stats(rets):
    eq, peak, mdd = 1.0, 1.0, 0.0
    for r in rets:
        eq *= (1 + r)
        peak = max(peak, eq)
        mdd = min(mdd, eq / peak - 1)
    wins = sum(1 for r in rets if r > 0)
    nz = sum(1 for r in rets if r != 0)
    return {'total': eq - 1, 'mdd': mdd,
            'win': wins / nz if nz else 0, 'days': nz, 'equity': eq}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--benchmark', default='US.QQQM')
    ap.add_argument('--lookback-days', type=int, default=400)
    ap.add_argument('--refresh', action='store_true', help='キャッシュを無視して再取得')
    ap.add_argument('--keep-lev-crypto', action='store_true',
                    help='レバ/逆張り・暗号ETFを除外せず残す(既定は除外)')
    args = ap.parse_args()

    start = (datetime.now() - timedelta(days=args.lookback_days)).strftime('%Y-%m-%d')
    end = datetime.now().strftime('%Y-%m-%d')

    print(f"\n{'='*72}\n  戦略バックテスト（1年・近似シグナル）\n{'='*72}")
    print(f"  期間: {start} 〜 {end}")

    import pickle
    cache = os.path.join(LOG_DIR, 'bt_cache.pkl')
    use_cache = (not args.refresh and os.path.exists(cache))
    if use_cache:
        with open(cache, 'rb') as fp:
            blob = pickle.load(fp)
        if blob.get('start') == start and blob.get('end') == end:
            groups, data = blob['groups'], blob['data']
            # キャッシュ生成後に追加したフィールドを再計算
            import statistics as _st
            patched = 0
            for code, days in data.items():
                for d, rec in days.items():
                    if 'sell_median' not in rec:
                        fm = rec.get('flow_median', 0.0)
                        wl = rec.get('window_len', 0)
                        pd_ = rec.get('pos_days', 0)
                        sd_ = wl - pd_
                        rec['sell_days'] = sd_
                        rec['sell_median'] = fm <= 0
                        rec['sell_strict'] = sd_ >= wl if wl < WEEK_WINDOW else sd_ >= wl - 1
                        patched += 1
            print(f"  キャッシュ使用: {len(data)}銘柄 ({cache})"
                  + (f"  /{patched}件フィールド補完" if patched else ""))
        else:
            use_cache = False
    if not use_cache:
        ctx = OpenQuoteContext(host=OPEND_HOST, port=OPEND_PORT)
        groups = {}
        for g in WATCHLISTS:
            r, d = ctx.get_user_security(g)
            groups[g] = d['code'].tolist() if r == RET_OK and not d.empty else []
            time.sleep(0.3)
        universe = sorted(set(c for v in groups.values() for c in v) | {args.benchmark})
        print(f"  対象銘柄: {len(universe)} (各2API → 約{len(universe)*2*CALL_INTERVAL/60:.0f}分)\n")
        data, fail = {}, []
        for i, code in enumerate(universe, 1):
            d = fetch_code_data(ctx, code, start, end)
            if d:
                data[code] = d
            else:
                fail.append(code)
            if i % 20 == 0:
                print(f"    取得 {i}/{len(universe)} ...", flush=True)
        ctx.close()
        with open(cache, 'wb') as fp:
            pickle.dump({'start': start, 'end': end, 'groups': groups, 'data': data}, fp)
        print(f"  取得完了: {len(data)}銘柄 / 失敗 {len(fail)}銘柄 (キャッシュ保存)")

    # レバ/逆張り・暗号ETFを除外（コモディティは残す）
    if not args.keep_lev_crypto:
        from moomoo import SecurityType, Market
        cx = OpenQuoteContext(host=OPEND_HOST, port=OPEND_PORT)
        re, ed = cx.get_stock_basicinfo(Market.US, SecurityType.ETF)
        cx.close()
        etf_names = {row['code']: row['name'] for _, row in ed.iterrows()} \
            if re == RET_OK and not ed.empty else {}
        total_ex = set()
        for g in WATCHLISTS:
            ex = build_excluded(etf_names, groups[g])
            groups[g] = [c for c in groups[g] if c not in ex]
            total_ex |= ex
        print(f"  レバ/逆張り・暗号ETF除外: {len(total_ex)}銘柄 "
              f"(例: {', '.join(sorted(total_ex)[:8])})")

    # マスターカレンダー = ベンチマークの営業日
    if args.benchmark not in data:
        print("  ベンチマークの日足が取れず中断")
        return
    calendar = sorted(data[args.benchmark].keys())

    # 各ウォッチリストをシミュレート
    print(f"\n{'─'*72}\n  結果（翌寄り執行・手数料無視）\n{'─'*72}")
    hdr = f"  {'対象':<14}{'リターン':>10}{'最大DD':>10}{'勝率':>8}{'取引数':>7}{'保有日':>7}"
    print(hdr)

    all_rets = None
    for g in WATCHLISTS:
        rets, trades, hd = simulate(groups[g], data, calendar)
        s = stats(rets)
        print(f"  {g:<14}{s['total']*100:>9.1f}%{s['mdd']*100:>9.1f}%"
              f"{s['win']*100:>7.0f}%{trades:>7}{hd:>7}")
        all_rets = rets if all_rets is None else [a + b for a, b in zip(all_rets, rets)]

    # 3ウォッチリスト均等(各1/3)
    combo = [r / len(WATCHLISTS) for r in all_rets]
    sc = stats(combo)
    print(f"  {'3つ均等合成':<14}{sc['total']*100:>9.1f}%{sc['mdd']*100:>9.1f}%"
          f"{sc['win']*100:>7.0f}%{'':>7}{'':>7}")

    # ベンチマーク(買い持ち, open-to-open)
    bcal = calendar
    bret = []
    for i in range(len(bcal) - 1):
        a, b = data[args.benchmark][bcal[i]], data[args.benchmark][bcal[i + 1]]
        bret.append(b['open'] / a['open'] - 1.0 if a['open'] and b['open'] else 0.0)
    sb = stats(bret)
    print(f"  {args.benchmark+'(買持)':<14}{sb['total']*100:>9.1f}%{sb['mdd']*100:>9.1f}%"
          f"{sb['win']*100:>7.0f}%{'':>7}{sb['days']:>7}")

    # === 2:1 整数株ポートフォリオ（改善版:米ハイテク2 / 標準版:米セクター1 / 予算=QQQM価格）===
    pr, pchg, pinv = simulate_portfolio(groups['米ハイテク'], groups['米国セクター'],
                                        data, calendar, args.benchmark)
    sp = stats(pr)
    print(f"\n{'─'*72}")
    print("  ◆ 2:1 整数株ポートフォリオ")
    print("    米ハイテク(改善版:median>0) × 2  +  米セクター(標準版:4/5日) × 1")
    print(f"    予算 = {args.benchmark} 1株価格 / 無signalは現金")
    start_cap = data[args.benchmark][calendar[1]]['open'] * 10
    print(f"  初期資金(={args.benchmark} 10株): ${start_cap:,.2f}")
    print(f"  {'戦略':<26}{'リターン':>10}{'最大DD':>10}{'勝率':>8}{'平均投資率':>10}{'変更':>7}")
    print(f"  {'2:1ポートフォリオ':<26}{sp['total']*100:>9.1f}%{sp['mdd']*100:>9.1f}%"
          f"{sp['win']*100:>7.0f}%{pinv*100:>9.0f}%{pchg:>7}")
    print(f"  {args.benchmark+' 買い持ち':<26}{sb['total']*100:>9.1f}%{sb['mdd']*100:>9.1f}%"
          f"{sb['win']*100:>7.0f}%{'100':>9}%{'':>7}")
    print(f"  → 最終評価額: 2:1=${start_cap*sp['equity']:,.0f}  /  "
          f"{args.benchmark}=${start_cap*(1+sb['total']):,.0f}")

    # エクイティCSV
    os.makedirs(LOG_DIR, exist_ok=True)
    path = os.path.join(LOG_DIR, 'backtest_strategy_equity.csv')
    eq_c = eq_b = 1.0
    import csv
    with open(path, 'w', newline='', encoding='utf-8') as fp:
        w = csv.writer(fp)
        w.writerow(['date', 'combo_equity', 'benchmark_equity'])
        for i in range(len(calendar) - 1):
            eq_c *= (1 + combo[i])
            eq_b *= (1 + bret[i])
            w.writerow([calendar[i + 1], round(eq_c, 5), round(eq_b, 5)])
    print(f"\n  日次エクイティ: {path}")
    print(f"\n  ※ 近似シグナル・生存者バイアス・手数料無視。参考値として解釈してください。\n")

    # === 売りシグナル検証 ===
    print(f"\n{'='*72}")
    print("  ◆ 売りシグナル検証（シグナル翌寄りで手放した後の価格推移）")
    print(f"{'='*72}")
    print(f"  {'対象':<14} {'n':>5}  {'翌日↓率':>8}  {'3日後↓率':>8}  {'5日後↓率':>8}"
          f"  {'翌日avg':>8}  {'3日avg':>8}  {'5日avg':>8}")
    print(f"  {'─'*74}")

    def _sell_fwd(codes, signal_key, label):
        """signal_key が True になった翌日寄りで手放し、その後N日の価格変化を集計。"""
        results = {'d1': [], 'd3': [], 'd5': []}
        for code in codes:
            if code not in data:
                continue
            cal_c = sorted(data[code].keys())
            for idx, di in enumerate(cal_c):
                rec = data[code][di]
                if not rec.get(signal_key, False):
                    continue
                # 売り執行日 = di+1 の open (翌寄り)
                if idx + 1 >= len(cal_c):
                    continue
                sell_day = cal_c[idx + 1]
                sell_price = data[code][sell_day]['open']
                if sell_price <= 0:
                    continue
                for offset, key in [(1, 'd1'), (3, 'd3'), (5, 'd5')]:
                    if idx + 1 + offset < len(cal_c):
                        fwd_day = cal_c[idx + 1 + offset]
                        fwd_open = data[code][fwd_day]['open']
                        if fwd_open > 0:
                            results[key].append(fwd_open / sell_price - 1.0)
        def _fmt(lst):
            if not lst:
                return f"{'—':>8}", f"{'—':>8}"
            down = sum(1 for v in lst if v < 0) / len(lst)
            avg  = sum(lst) / len(lst)
            return f"{down*100:>7.0f}%", f"{avg*100:>+7.2f}%"
        n = len(results['d1'])
        d1_d, d1_a = _fmt(results['d1'])
        d3_d, d3_a = _fmt(results['d3'])
        d5_d, d5_a = _fmt(results['d5'])
        print(f"  {label:<14} {n:>5}  {d1_d}  {d3_d}  {d5_d}  {d1_a}  {d3_a}  {d5_a}")

    hi_codes  = [c for c in groups['米ハイテク']  if c not in (getattr(args, '_ex', set()))]
    se_codes  = [c for c in groups['米国セクター'] if c not in (getattr(args, '_ex', set()))]

    _sell_fwd(hi_codes,  'sell_median', '米ハイテク(中央値売り)')
    _sell_fwd(se_codes,  'sell_strict', '米セクター(4/5売り)')
    _sell_fwd(hi_codes,  'sell_strict', '米ハイテク(4/5売り・参考)')
    _sell_fwd(se_codes,  'sell_median', '米セクター(中央値売り・参考)')

    print(f"\n  ↓率: シグナル翌寄り売却後、その価格を下回った割合（高いほど売りが正確）")
    print(f"  avg: 翌寄り売値からの平均変化率（マイナスほど「売って正解」）\n")


if __name__ == '__main__':
    main()
