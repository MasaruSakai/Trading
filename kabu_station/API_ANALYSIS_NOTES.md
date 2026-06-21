# kabu Station API analysis notes

Last checked: 2026-06-21

Sources:

- Developer portal: https://kabucom.github.io/kabusapi/ptal/
- REST API reference: https://kabucom.github.io/kabusapi/reference/index.html
- OpenAPI YAML: https://github.com/kabucom/kabusapi/blob/master/reference/kabu_STATION_API.yaml

## Core constraints

- Base URL is `http://localhost:18080/kabusapi` for production and
  `http://localhost:18081/kabusapi` for test.
- Most endpoints require `X-API-KEY`.
- The token is issued by `POST /token` and becomes invalid when kabu Station
  exits, logs out, or another token is issued.
- Information APIs have a rough limit of 10 requests/second.
- Order APIs have a rough limit of 5 requests/second.
- Wallet APIs have a rough limit of 10 requests/second.
- Information requests such as `/board/{symbol}` automatically register the
  requested symbol into the API registered-symbol list.
- The registered-symbol list is limited to 50 symbols across REST and PUSH.
  This is the likely cause of `4002006 レジスト数エラー` when trying to run
  board analysis across many ETFs.

## Endpoint summary for analysis

### `GET /ranking`

Use this first for universe selection. It does not require per-symbol board
registration and gives enough data for broad screening.

Query parameters:

- `Type`: ranking type. Required.
- `ExchangeDivision`: market. Required.
  - `ALL`: all markets
  - `T`: all TSE
  - `TP`: TSE Prime
  - `TS`: TSE Standard
  - `TG`: Growth 250
  - `M`: Nagoya
  - `FK`: Fukuoka
  - `S`: Sapporo

Important `Type` values:

| Type | Meaning | Use in analysis |
| --- | --- | --- |
| `1` | 値上がり率 | momentum/upside screen |
| `2` | 値下がり率 | weak names, reversal candidates |
| `3` | 売買高上位 | liquidity by volume |
| `4` | 売買代金 | main liquidity universe |
| `5` | TICK回数 | intraday activity / market attention |
| `6` | 売買高急増 | volume surge |
| `7` | 売買代金急増 | turnover surge |
| `8` | 信用売残増 | short balance increase |
| `9` | 信用売残減 | short balance decrease |
| `10` | 信用買残増 | margin long increase |
| `11` | 信用買残減 | margin long decrease |
| `12` | 信用高倍率 | high margin ratio |
| `13` | 信用低倍率 | low margin ratio |
| `14` | 業種別値上がり率 | sector strength |
| `15` | 業種別値下がり率 | sector weakness |

Important response fields:

- `Symbol`, `SymbolName`
- `CurrentPrice`
- `ChangeRatio`
- `ChangePercentage`
- `TradingVolume`
  - In ranking responses this is shown in thousand-share units.
- `Turnover`
  - In ranking responses this is shown in million-yen units.
- `TickCount`, `UpCount`, `DownCount`
  - Only for Type `5`.
- `RapidTradePercentage`
  - For Type `6`.
- `RapidPaymentPercentage`
  - For Type `7`.
- `ExchangeName`
- `CategoryName`
- `Trend`
  - Relative ranking trend versus the past 10 business days.
- `AverageRanking`
  - Average rank. Values worse than top 100 are represented as `999`.

Current correction for our prototype:

- `Type=5` is not turnover. It is TICK count.
- Use `Type=4` for pure売買代金 universe.
- Use `Type=7` for売買代金急増.
- If we want "売買代金上位30 ETF", start with `Type=4`, filter ETF/ETN,
  then optionally enrich with `Type=7` or `Type=5`.

### `GET /board/{symbol}`

Use this only for a small, final set of symbols because it consumes the
registered-symbol list.

Path:

- `{symbol}` must be `[銘柄コード]@[市場コード]`.
- Relevant market codes:
  - `1`: 東証
  - `3`: 名証
  - `5`: 福証
  - `6`: 札証
  - `2`: 日通し
  - `23`: 日中
  - `24`: 夜間

Useful response fields:

- Price:
  - `CurrentPrice`
  - `CurrentPriceTime`
  - `CurrentPriceStatus`
  - `CalcPrice`
  - `PreviousClose`
  - `ChangePreviousClose`
  - `ChangePreviousClosePer`
  - `OpeningPrice`
  - `HighPrice`
  - `LowPrice`
- Liquidity:
  - `TradingVolume`
  - `TradingVolumeTime`
  - `VWAP`
  - `TradingValue`
- Board pressure:
  - `MarketOrderSellQty`
  - `MarketOrderBuyQty`
  - `Sell1` ... `Sell10`
  - `Buy1` ... `Buy10`
  - `OverSellQty`
  - `UnderBuyQty`
- Metadata:
  - `Symbol`
  - `SymbolName`
  - `Exchange`
  - `ExchangeName`
  - `SecurityType`

Important caution:

- The official reference notes that `Bid` and `Ask` key names are reversed
  from their usual English meaning. The Japanese descriptions are authoritative:
  - `BidQty`, `BidPrice` are 最良売気配.
  - `AskQty`, `AskPrice` are 最良買気配.
- Our current code mainly uses `Sell*` and `Buy*`, which are clearer.

Recommended use:

- Do not call `/board` for hundreds or thousands of symbols.
- Prefer:
  1. `/ranking` for broad universe.
  2. Select top 20-50.
  3. Call `/board` only for selected names and holdings.

### `GET /positions`

Use this for holdings. This should be the main source for "保有銘柄" output.

Query parameters:

- `product`
  - `0`: all
  - `1`: spot
  - `2`: margin
  - `3`: futures
  - `4`: options
- `symbol`: optional single-symbol filter.
- `side`
  - `1`: sell
  - `2`: buy
- `addinfo`
  - Default is true.
  - When true, it adds current price, valuation, profit/loss, and
    profit/loss rate.

Useful response fields:

- `Symbol`
- `SymbolName`
- `Exchange`
- `ExchangeName`
- `Price`
  - Position price / acquisition price.
- `LeavesQty`
  - Holding quantity.
- `HoldQty`
  - Quantity locked for settlement/repayment.
- `Side`
  - `1`: sell
  - `2`: buy
- `CurrentPrice`
- `Valuation`
- `ProfitLoss`
- `ProfitLossRate`

Recommended use:

- Holdings judgment should be position-centric, not board-centric.
- For holdings:
  - Always show `ProfitLossRate`, `ProfitLoss`, `Valuation`, `LeavesQty`.
  - Use `/board` only as an optional short-term pressure overlay.
  - If `/board` fails due to registered-symbol limits, still output the
    holding from `/positions`.

### `GET /symbol/{symbol}`

Use this for symbol metadata when needed.

Useful response fields:

- `Symbol`
- `SymbolName`
- `DisplayName`
- `Exchange`
- `ExchangeName`
- `BisCategory`
- Additional info is controlled by `addinfo`.

This is useful for classification, but it is still an information endpoint and
can contribute to registered-symbol pressure. Prefer ranking metadata first.

### `GET /apisoftlimit`

Returns soft limit values and kabu Station version.

Useful fields:

- `Stock`
- `Margin`
- `Future`
- `FutureMini`
- `FutureMicro`
- `Option`
- `MiniOption`
- `KabuSVersion`

This is more useful for order sizing than market analysis.

## Recommended prototype logic

### Universe selection

Use ranking-first logic:

1. Main ETF candidate set:
   - `GET /ranking?Type=4&ExchangeDivision=ALL`
   - Filter `ExchangeName` containing `ETF` or `ETN`.
   - Sort by `Turnover` descending.
2. Momentum supplement:
   - `Type=7` for売買代金急増.
   - `Type=5` forTICK回数, if we want activity rather than value.
   - These should enrich symbols already selected by `Type=4`; they should
     not expand the extraction universe by themselves.
3. Keep at most 30 ETF candidates for display.

Do not treat `Type=5` as売買代金. It is only activity count.

### ETF candidate scoring

Split the score into two layers.

Ranking score:

- liquidity: `Turnover`
- momentum: `ChangePercentage`
- activity: `TickCount`, `UpCount`, `DownCount` from Type `5`
- surge: `RapidPaymentPercentage` from Type `7`

Board score, only when `/board` succeeds:

- `CurrentPrice / VWAP - 1`
- `TradingValue`
- `MarketOrderBuyQty - MarketOrderSellQty`
- depth pressure from `Buy1..Buy10` and `Sell1..Sell10`
- optional OVER/UNDER pressure from `UnderBuyQty` and `OverSellQty`

Display board-derived names separately from ranking-only names:

- `ETF買い候補(board判定)`
- `ETF参考(rankingのみ)`

This avoids a misleading list where many rows have neutral `0.000` board score.

### Holdings judgment

Use `/positions` as the primary source.

Suggested labels:

- `継続`
  - `ProfitLossRate >= 0`
  - and board pressure is not clearly negative.
- `利確候補`
  - `ProfitLossRate >= 8%`
  - especially if board pressure or current momentum is weakening.
- `注意`
  - `ProfitLossRate < 0`
  - or board score is negative.
- `売却注意`
  - `ProfitLossRate <= -3%`
  - or `CurrentPrice < VWAP` and board pressure is negative.

For holdings, do not drop rows when `/board` fails. The positions data is more
important than board availability.

### Request strategy

Recommended order:

1. `/positions?product=1&addinfo=true`
2. `/ranking?Type=4&ExchangeDivision=ALL`
3. Optional supplement rankings:
   - `Type=7`
   - `Type=5`
4. Call `/board` only for:
   - current holdings
   - top ETF candidates after ranking filtering
5. Stop before registered-symbol count approaches 50.

Because `/board` and other information requests auto-register symbols, broad
board scans are structurally incompatible with the API's 50-symbol registration
limit.

## Immediate code implications

- Change ETF universe from `Type=5` to `Type=4`.
- Keep `Type=5` only as an activity supplement.
- Keep `Type=7` as turnover-surge supplement.
- Avoid filling 30 ETFs by calling `/board` on many unsupported or low-priority
  symbols.
- For holdings, output `/positions` data regardless of `/board` result.
- Consider adding a cleanup step with `/unregister/all` before a fresh analysis
  run, if it does not interfere with other PUSH/REST usage.
