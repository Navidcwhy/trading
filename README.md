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
| Scout | BTC, ETH + topp 15 alts (spot + USD-M perps) 24/7. Guld (XAUUSDT) och silver (XAGUSDT) som perps på Binance, 24/7; där perps är avstängda (molnet) handlas guld som token på Binance spot (XAUT, annars PAXG; en token = ett troy ounce guld), 24/7. US-aktier och fonder under ordinarie börstid, 15:30–22:00 svensk tid (`stocks_session`): bevakningslistan QQQ (Nasdaq-100), GLD (guld), SLV (silver) och EWD (svenska storbolag) varje cykel, plus most actives och gainers; hävstångs- och blankningsfonder och warranter ingår medvetet. Tokeniserade aktier på Binance (bStocks, t.ex. MUB = Micron) och övriga TradFi-perps (aktier, olja, index) räknas inte som krypto och utesluts. Om Binances lista över bStocks inte går att hämta används senast sparade kopia; finns ingen, bara BTC/ETH | `universe` |
| Filter | Dödar: spread för bred · earnings idag/imorgon · funding överhettad · volym under 0,6 × 30d-snittet (aktier: mot vad en normal dag har handlat vid samma tid) · tunn orderbok · öppen nyhetsrisk (krypto: RSS, aktier: Finnhub, guld/silver: Finnhubs allmänna marknadsnyheter + Binance-nyheter). Uppföljande rubriker om kända, redan hanterade händelser spärrar inte fram till ett slutdatum (`news_acknowledged`). Just nu gäller det två händelser: Binance-hacket 29 okt, t.o.m. 16 nov (rubriker som nämner "Binance hack" eller $6,3 miljarder), och MEXC, t.o.m. 17 nov (rubriker som nämner MEXC; den 10 nov stängde FinCEN ute MEXC från det amerikanska finanssystemet och Binance stoppade överföringar till MEXC). Andra riskord i samma rubrik, t.ex. stoppade uttag, spärrar fortfarande. Det gör även tecken på ett nytt hack ("hacked again", "another hack") och rubriker där Binance stoppar sina egna uttag, insättningar eller sin handel, eller där myndigheter riktar sig mot Binance självt. **Fail-closed**: saknas data dör kandidaten | `filter` |
| Analyst | Poäng 0–1 (trend, momentum, RSI, relativ volym, volatilitet mot stoppen, spread, orderboksobalans, funding). Under 0,6 dör | `analyst` |
| Executor | Max 10 % av equity per trade. Respekterar börsens minsta ordervärde. Long-only, perps 1x. Hävstång bara i paper, för tillgångar som rör sig lite: 2x eller 3x på 5 % marginal (se Hävstång) | `executor`, `risk.leverage` |
| Risk | −8 % stop sätts före entry. Trailar på topp −8 % efter +15 %. Max 10 positioner, en position per tillgång, daglig drawdown-spärr −5 %, equity-golv 80 % av start ($160 vid $200), stänger aktier 10 min innan börsen stänger (ingen aktie över natten). Hävstångsaffärer får tätare stop, vinsthemtagning och trail. Tre valfria säljregler är avstängda här och påslagna på daytrade-desken: vinsthemtagning, längsta innehavstid och spärrtid före återköp (se Två deskar) | `risk` |
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

## Köra i molnet (Claude-session)

```
bash cloud/start.sh     # startar båda deskarna + snapshot-loopen; säker att köra igen
bash cloud/health.sh    # läget: processer, senaste cykel, affärer, jämförelse
```

- **Geospärr:** molnet når inte `api.binance.com` eller `fapi.binance.com` (HTTP 451). Därför går
  spot-data via Binances spegling `data-api.binance.vision`, och perps är avstängda
  (`DESK_BINANCE_SPOT_DATA` och `DESK_PERPS_OFF` i `.env`, som `start.sh` skriver). Guld handlas då
  som token på spot (XAUT/PAXG); silver finns bara som SLV under amerikansk börstid. Agenterna och
  reglerna är desamma. Båda deskarna har samma universum, så jämförelsen mellan dem är fortfarande
  rättvis. På en dator i Sverige behövs ingen av inställningarna.
- **Överlevnad:** böckerna sparas var 10:e minut till branchen `claude/trading-desk-state`, som har
  en enda commit som skrivs över. I en ny container återställer `start.sh` dem automatiskt. Det man
  kan förlora är upp till 10 minuters historik, och stopparna bevakas inte medan containern är nere.
- **Nödstopp:** som vanligt, med `STOP`, `FLATTEN`, `STOP.daytrade` och så vidare i projektmappen.

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

- **Börstid:** aktier scoutas under ordinarie börstid, 09:30–16:00 i New York (15:30–22:00 svensk
  tid, kortare vissa halvdagar). Inga nya aktieköp de sista 20 minuterna, och alla aktiepositioner
  stängs 10 min före stängning. Ingen aktie ligger kvar över natten.
- **Bevakningslistan** (`stocks_watchlist`) scoutas varje cykel, oavsett om de är bland dagens mest
  handlade: QQQ (Nasdaq-100), GLD (guld), SLV (silver) och EWD (iShares MSCI Sweden, svenska
  storbolag i dollar). Siffran efter symbolen är tillgången den räknas som. GLD räknas som guld,
  så desken håller aldrig både GLD och guldtoken samtidigt.
- **OMXS30** går inte att handla direkt här: indexet kräver en ny datakälla (Yahoo Finance, som inte
  är tillåten i molnmiljön) eller Finnhubs betalplan. EWD är närmaste ersättare, men det är inte
  samma sak: andra vikter, i dollar och bara 15:30–22:00.
- **Volym:** dagens volym jämförs med vad en normal dag har handlat vid samma tid. Kurvan är
  U-formad: mycket i början, lugnt mitt på dagen, mest i slutet. Klockan 18:45 svensk tid förväntas
  ungefär halva dagens volym.
- **Kursflödet (IEX)** är gratisplanens: under börstid färskt, efter stängning sällan uppdaterat
  (18 nov köptes AES på en 1,5 h gammal after-hours-kurs). Filter dödar därför aktier vars senaste
  bud/utbud är äldre än 5 min (`filter.max_quote_age_sec`), och därför handlas inte after-hours.
  IEX visar bara sin egen del av orderboken, ofta 100 aktier, så gränsen för tunn orderbok för
  aktier är $2 000. Det räcker gott för ordrar på $10–30.
- Alpaca tillåter bara limit-ordrar och inga stop-ordrar i extended hours. Därför övervakas stoppar
  för aktier av Risk.

## Hävstång (bara paper)

Hävstång används när den behövs: för tillgångar som rör sig så lite att en vanlig affär knappt
märks, t.ex. guld, Nasdaq-fonden och silver. Vanlig krypto handlas utan hävstång.

- **Hur mycket:** 1,5 % delat med tillgångens genomsnittliga timrörelse (1h ATR), avrundat nedåt,
  högst 3x. Guld rör sig ca 0,3–0,5 % i timmen och får 3x. BTC rör sig ca 0,6–0,8 % och får 2x.
  Altcoins rör sig 1 % eller mer och får ingen hävstång.
- **Mindre pengar:** en hävstångsaffär binder 5 % av equity i stället för 10 %. Positionen blir
  marginalen gånger hävstången: 10 % av equity vid 2x, 15 % vid 3x.
- **Tätare stop:** vid 3x blir alla prisgränser två tredjedelar så långa. På huvuddesken blir stopen
  −5,33 % i stället för −8 %, och trailen startar vid +10 % och följer 5,33 % under toppen. På
  daytrade-desken blir stopen −2 % och vinsthemtagningen +3 %. 2x ger samma position som en vanlig
  affär och därför samma gränser, men med halva pengarna.
- **Samma risk per affär:** en hävstångsaffär kan aldrig förlora mer vid stopen än en vanlig affär
  på 10 % vid −8 % (huvuddesken $1,60, daytrade $0,60 vid $200).
- **Kostnad och spärrar:** ränta på den lånade delen, 10 % per år för krypto och 8 % för aktier,
  dras varje timme och vid försäljning. Alla öppna positioner får tillsammans vara högst 150 % av
  equity. En position vars marginal nästan är slut stängs (`margin_call_pct`), men det är bara ett
  skyddsnät, eftersom stopen ligger långt före.
- **Bara paper:** i testnet och live handlar Executor alltid utan hävstång, och Risk vägrar
  hävstångsordrar. Inställningar: `risk.leverage` (`max_x: 1` stänger av).

## Ärliga begränsningar

- **$200 och 10 % = ca $20 per trade.** Binance kräver minst $5 per order för de flesta instrument,
  men $50 för BTC-perps och $20 för vissa andra perps (t.ex. ETH och LINK). BTC-perps går därför
  aldrig att handla med den här storleken, och $20-instrumenten ligger precis på gränsen. När en
  desk har tio öppna positioner finns bara några dollar kvar, och då dödas nya köp av Risk (max
  antal positioner) eller av Executor (ordern blir för liten).
- Analystens poäng är en genomskinlig heuristik, inte en backtestad edge. Bedöm den på
  flera veckors paper-resultat, inte på en natt.
- Volymregeln dödade fram till 19 nov allt under 30-dagarssnittet: 79 % av alla avslag, och nästan
  allt på helger och lugna dagar (median 0,74x). Nu är gränsen 0,6x, som bara stoppar döda marknader.
  Hög volym belönas fortfarande av Analysten (relativ volym i poängen).
- Nyhetsfiltret söker på ord: "Ethereum block builders received the most in a $3.4 million vault
  hack" stoppar ETH i upp till 12 h, fast hacket inte gällde Ethereum.
- Guld och silver rör sig sällan 8 % eller 15 %, så sådana positioner kan bli liggande länge.
  Hävstången (3x, två tredjedelar så långa gränser) gör det mindre, men tar inte bort det.
  Nyhetskollen för metallerna fångar bara stora rubriker i Finnhubs allmänna nyhetsflöde;
  räntebesked och inflationssiffror filtreras inte. Med $20 per trade blir guldpositionen ca $16,
  eftersom kontraktet bara kan köpas i steg om 0,001 guld (ca $4).
- Stoppar i paper är på klientsidan: de kollas var 15:e sekund, så snabba gap kan ge sämre fyllning än −8 %.
