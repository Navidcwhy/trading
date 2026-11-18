# Desk – autonomt trading-desk (aktier + krypto)

Sex agenter med hårda överlämningar. Ingen agent kan hoppa över en annan:

```
Scout ──Candidate──▶ Filter ──Survivor──▶ Analyst ──Signal──▶ Executor ──OrderTicket──▶ Risk
                                                                  ▲                      │
                                                                  └──ApprovedOrder(sigill + stop)┘
alla ──kill/event──▶ Logger (SQLite + JSONL + konsol)
```

Executor lägger aldrig en order utan ett HMAC-sigill som bara Risk kan skapa, och sigillet
innehåller stoppen. Alltså finns stoppen innan ordern går iväg.

| Agent | Gör | Regler (config.json) |
|---|---|---|
| Scout | BTC, ETH + topp 15 alts (spot + USD-M perps) 24/7. Guld (XAUUSDT) och silver (XAGUSDT) som perps på Binance, 24/7. US-aktier (most actives + gainers) i after-hours; hävstångs- och blankningsfonder och warranter ingår medvetet. Tokeniserade aktier på Binance (bStocks, t.ex. MUB = Micron) och övriga TradFi-perps (aktier, olja, index) räknas inte som krypto och utesluts. Om Binances lista över bStocks inte går att hämta används senast sparade kopia; finns ingen, bara BTC/ETH | `universe` |
| Filter | Dödar: spread för bred · earnings idag/imorgon · funding överhettad · volym < 30d-snitt · tunn orderbok · öppen nyhetsrisk (krypto: RSS, aktier: Finnhub, guld/silver: Finnhubs allmänna marknadsnyheter + Binance-nyheter). Uppföljande rubriker om kända, redan hanterade händelser spärrar inte fram till ett slutdatum (`news_acknowledged`). Just nu gäller det två händelser: Binance-hacket 29 okt, t.o.m. 16 nov (rubriker som nämner "Binance hack" eller $6,3 miljarder), och MEXC, t.o.m. 17 nov (rubriker som nämner MEXC; den 10 nov stängde FinCEN ute MEXC från det amerikanska finanssystemet och Binance stoppade överföringar till MEXC). Andra riskord i samma rubrik, t.ex. stoppade uttag, spärrar fortfarande. Det gör även tecken på ett nytt hack ("hacked again", "another hack") och rubriker där Binance stoppar sina egna uttag, insättningar eller sin handel, eller där myndigheter riktar sig mot Binance självt. **Fail-closed**: saknas data dör kandidaten | `filter` |
| Analyst | Poäng 0–1 (trend, momentum, RSI, relativ volym, volatilitet mot stoppen, spread, orderboksobalans, funding). Under 0,6 dör | `analyst` |
| Executor | Max 10 % av equity per trade. Respekterar börsens minsta ordervärde. Long-only, perps 1x | `executor` |
| Risk | −8 % stop sätts före entry. Trailar på topp −8 % efter +15 %. Max 10 positioner, en position per tillgång, daglig drawdown-spärr −5 %, equity-golv 80 % av start ($160 vid $200), stänger aktier innan after-hours slutar. Tre valfria säljregler är avstängda här och påslagna på daytrade-desken: vinsthemtagning, längsta innehavstid och spärrtid före återköp (se Två deskar) | `risk` |
| Logger | Varje kill med uppmätt värde mot tröskel. Varje trade, stop-flytt och fel | `data/<mode>/desk.db`, `logs/` (daytrade-desken: `data/paper-daytrade/`, `logs/daytrade/`) |

## Kommandon

```bash
python -m desk run            # kör loopen (skanning var 5:e min, stopp-övervakning var 15:e s)
python -m desk once           # en skanning, sedan avslut
python -m desk report         # equity, positioner, trades, kills per regel
python -m desk report 12      # bara de senaste 12 timmarna
python -m desk kills 50       # de senaste 50 kills
python -m desk --profile daytrade run      # daytrade-desken; --profile fungerar med alla kommandon ovan
python -m desk compare        # deskarna bredvid varandra, räknat från att den senaste startade
python -m unittest tests.test_desk   # offline-tester av risk- och handoff-reglerna
```

`run_desk.bat` startar huvuddesken och `run_daytrade.bat` daytrade-desken (samma sak som
`run_desk.bat daytrade`). Dubbelklicka på var och en, så får de var sitt fönster. Båda startar om
automatiskt om de kraschar. Stäng fönstret för att stoppa en desk.

**Nödstopp:** skapa en tom fil `STOP` i mappen, så öppnar ingen desk nya positioner.
Skapa `FLATTEN`, så stängs allt. Ta bort filen för att fortsätta. För att bara träffa en desk
lägger du till deskens namn: `STOP.daytrade`, `FLATTEN.daytrade`, `STOP.main` eller `FLATTEN.main`.
Det gör inget om Windows lägger till `.txt` i slutet av filnamnet.

## Flyttad till GitHub (2026-11-18)

Koden låg tidigare bara i en scratch-mapp på Windows-datorn. Den är återställd från sessionens
transkript och verifierad: alla 41 tester går igenom, och varje fil stämmer med sessionens sista
fulla läsning. Följande kom **inte** med:

- **Papperböckerna** (`data/`). Huvuddesken hade 9 positioner och var −2,6 % ($194,87), och
  daytrade-desken hade 1 position och var +0,03 % ($200,05), båda 10 nov kl. 19:42. Båda deskarna
  startar om på $200, och veckojämförelsen börjar om från dag 0.
- **`.env`**. Fyll i nycklarna igen: `cp .env.example .env`. Krypto kräver inga nycklar, men aktier
  kräver Alpaca (paper) och Finnhub.
- **Loggarna** (`logs/`).

Starta på en ny dator (Python 3.12+, inga paket behövs):

```
git clone https://github.com/navidcwhy/trading && cd trading
git checkout claude/trading-desk-migration-20pmkj
python -m unittest tests.test_desk          # 41 tester, ska vara OK
cp .env.example .env                        # fyll i nycklar själv
./run_desk.sh & ./run_desk.sh daytrade &    # Windows: run_desk.bat och run_daytrade.bat
python -m desk compare                      # jämför deskarna
```

Datorn behöver nå `api.binance.com`, `fapi.binance.com`, `www.binance.com`, `cointelegraph.com`,
`www.coindesk.com`, `decrypt.co`, `finnhub.io`, `data.alpaca.markets` och `paper-api.alpaca.markets`.
Filtret är fail-closed: om nyhetsflödena inte går att nå spärras alla köp.

## Två deskar sida vid sida

Två paper-deskar körs samtidigt. De har samma agenter, samma filter, samma poängsättning och $200
var. Det enda som skiljer är Risks säljregler:

| | Huvuddesken | Daytrade-desken |
|---|---|---|
| Startas med | `python -m desk run` | `python -m desk --profile daytrade run` |
| Stop | −8 % | −3 % |
| Vinsthemtagning | ingen; trailar 8 % under toppen efter +15 % | säljer vid +4,5 % |
| Längsta innehav | ingen gräns | 8 timmar |
| Spärrtid efter försäljning | ingen | 120 minuter |
| Inställningar | `config.json` | `config.json` + `config.daytrade.json` |
| Bok och loggar | `data/paper/`, `logs/` | `data/paper-daytrade/`, `logs/daytrade/` |

`config.daytrade.json` innehåller bara skillnaderna. Allt annat ärvs från `config.json`, så en ändring
där (t.ex. i filtret) gäller båda deskarna efter omstart. En tredje desk är en ny fil
`config.<namn>.json` som startas med `--profile <namn>`.

- Varje desk är en egen process med egen bok. Startar du samma desk en gång till vägrar den andra
  processen att starta, och en desk som körs går inte att nollställa med `reset-paper`.
- Deskar med `--profile` går bara i paper. Bara huvuddesken kan gå vidare till testnet och live.
- **Spärrtiden** finns för att signalerna kommer tillbaka var femte minut. Utan den köper desken
  tillbaka samma tillgång direkt efter en försäljning och betalar avgifter en gång till. Spärren
  gäller tillgången, alltså både spot och perp. `reentry_cooldown_min: 0` stänger av den.
- Efter avgifter och slippage ger en vinsthemtagning ungefär +4,3 % och en stop ungefär −3,2 % av
  positionens värde. Om alla affärer slutade på något av de två sätten skulle daytrade-desken behöva
  vinna drygt 4 av 10 för att gå jämnt upp. Affärer som stängs på tid hamnar däremellan.

`python -m desk compare` ställer deskarna bredvid varandra. Allt räknas från den stund den senast
startade desken kom igång, så att båda mäts över samma tid: equity då och nu, förändring i procent
(raden `Change`), öppna positioner, andel investerat, antal köp, stängda affärer, vinnare,
realiserat resultat, snittinnehav, avgifter och vad som stängde affärerna.

Jämför på `Change`, men läs den ihop med `Invested`. Huvuddesken gick in i jämförelsen nästan fullt
investerad, daytrade-desken med bara kontanter. Den desk som har minst pengar i marknaden ser bäst ut
när marknaden faller och sämst när den stiger, vilka regler den än har. Ge det minst en vecka.

## Lägen: paper → testnet → live

1. **paper** (standard): fyller mot riktiga live-priser med avgifter och slippage. Inga nycklar för krypto.
2. **testnet**: riktiga ordrar mot börsernas *testmiljöer* (Binance testnet, Alpaca paper-API).
   Sätt `DESK_MODE=testnet` och testnet-nycklar i `.env`, och kör `python -m desk preflight`.
3. **live**: kräver att testnet-preflight har gått igenom, `DESK_LIVE_CONFIRM=I_ACCEPT_REAL_MONEY_RISK`
   i `.env` och en Binance-nyckel **utan uttagsrätt** (preflight vägrar annars). Det steget gör du själv.

Live/testnet-adaptrarna är **inte verifierade** mot riktiga konton. Det är därför testnet-steget finns.

## Aktier

Kräver gratisnycklar i `.env` (se `.env.example`): Alpaca paper-konto (kurser, kalender) och
Finnhub (earnings och nyheter). Utan Finnhub dödas alla aktier av earnings-regeln (fail-closed).
Alpaca tillåter bara limit-ordrar och inga stop-ordrar i extended hours. Därför övervakas stoppar
för aktier av Risk, och aktiepositioner stängs 10 min innan after-hours slutar.

## Ärliga begränsningar

- **$200 och 10 % = ca $20 per trade.** Binance kräver minst $5 per order för de flesta instrument,
  men $50 för BTC-perps och $20 för vissa andra perps (t.ex. ETH och LINK). BTC-perps går därför
  aldrig att handla med den här storleken, och $20-instrumenten ligger precis på gränsen. När en
  desk har tio öppna positioner finns bara några dollar kvar, och då dödas nya köp av Risk (max
  antal positioner) eller av Executor (ordern blir för liten).
- Analystens poäng är en genomskinlig heuristik, inte en backtestad edge. Bedöm den på
  flera veckors paper-resultat, inte på en natt.
- Volymregeln (24 h < 30-dagarssnittet) dödar nästan allt på helger.
- Guld och silver rör sig sällan 8 % eller 15 %, så sådana positioner kan bli liggande länge.
  Nyhetskollen för metallerna fångar bara stora rubriker i Finnhubs allmänna nyhetsflöde;
  räntebesked och inflationssiffror filtreras inte. Med $20 per trade blir guldpositionen ca $16,
  eftersom kontraktet bara kan köpas i steg om 0,001 guld (ca $4).
- Stoppar i paper är på klientsidan: de kollas var 15:e sekund, så snabba gap kan ge sämre fyllning än −8 %.
