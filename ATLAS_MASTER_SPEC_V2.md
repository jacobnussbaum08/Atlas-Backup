# ATLAS AI TRADER
## MASTER ARCHITECTURE & IMPLEMENTATION SPECIFICATION
### Version 2.0 — Consolidated Technical Blueprint

**Project:** Atlas AI Trader  
**Document purpose:** Authoritative architecture, engineering requirements, development roadmap, and implementation instructions.  
**Primary development environment:** VS Code + Claude Code  
**Initial deployment:** Local machine using Docker Compose  
**Initial operating mode:** Fully autonomous paper trading  
**Initial simulated capital:** $100,000 USD

---

# 0. AUTHORITY OF THIS DOCUMENT

This document supersedes all previous Atlas AI Trader specifications.

All development decisions must be consistent with this document unless a deliberate architectural revision is documented and approved.

The coding AI must not silently remove requirements, replace real functionality with mock implementations, or introduce architectural changes simply because they are easier to implement.

When a requirement is technically unsuitable, identify the problem, explain the reason, and propose a replacement before changing the architecture.

Atlas must be developed incrementally. A functioning, thoroughly tested foundation is more valuable than a large collection of unfinished modules.

**The initial project is strictly paper-trading-only. Real-money brokerage execution is outside the scope of this specification.**

# 1. PROJECT VISION

Build Atlas AI Trader as an autonomous, multi-market quantitative trading research and execution platform.

Atlas should continuously analyze eligible financial markets, identify potential trading opportunities, evaluate competing strategies, manage simulated portfolio exposure, execute approved paper trades, and improve its analytical models through controlled experimentation.

The system should support:

### Markets
- Individual stocks
- Exchange-traded funds
- Forex
- Cryptocurrency

### Trading approaches
- Intraday trading
- Day trading
- Swing trading
- Longer-term position trading

### Trading directions
- Long
- Short

### Analytical methods
- Technical analysis
- Quantitative analysis
- Statistical modeling
- Machine learning
- Market-regime detection
- News and event analysis
- Cross-asset relationships

Atlas should not depend on one indicator, one strategy, one AI model, or one market condition.

Its job is not to trade constantly. Its job is to identify opportunities with a measurable advantage, evaluate them against portfolio conditions, and take risk only when the evidence and risk controls justify it.

A no-trade decision is completely valid.

# 2. FINAL PROJECT DECISIONS

These decisions are established and should not be repeatedly reconsidered during ordinary implementation.

| Category | Final decision |
|---|---|
| Name | Atlas AI Trader |
| Markets | Stocks, ETFs, forex, crypto |
| Trading style | Combination of intraday, swing, and position trading |
| Asset discovery | Dynamic |
| Market scanning | Continuous, event-driven |
| News | Supporting analytical signal |
| Trading personality | Balanced |
| Position direction | Long and short |
| Automation | Fully autonomous paper trading |
| Simulated capital | $100,000 |
| Initial risk per trade | 0.5% of equity |
| Maximum simultaneous positions | 8 |
| Daily loss limit | 2% |
| Weekly loss limit | 5% |
| Maximum drawdown threshold | 10% |
| Initial leverage | 1x |
| Hosting | Local first, cloud after validation |
| Budget | Free/low-cost initially |
| AI architecture | External AI APIs plus locally trained ML models |
| Paper-trading graduation | Performance requirements, not a fixed date |
| Live trading | Not implemented or enabled |

The financial limits are initial development parameters. They must be configurable, versioned, tested, and protected from autonomous modification.

# 3. CORE ENGINEERING PRINCIPLES

## 3.1 Modular monolith, not premature microservices

Atlas will initially use a **modular monolithic architecture with separate background workers**.

All major systems will share one codebase and clearly defined interfaces, but run as separate logical modules.

This provides:
- Easier development
- Less infrastructure overhead
- Simpler local debugging
- Consistent database transactions
- Lower initial operating costs
- Clear module boundaries
- An easier path to future service separation

Do not create dozens of independently deployed microservices during the initial build.

The architecture should remain capable of separating high-load modules into independent services later if necessary.

## 3.2 Database as the source of truth

PostgreSQL must be the authoritative source for transactional state.

Redis may provide:
- Caching
- Background-task coordination
- Transient state
- Messaging support

Redis must never become the authoritative source for:
- Account balances
- Orders
- Positions
- Fills
- Risk reservations
- Financial ledger entries

Critical state must be recoverable from PostgreSQL.

## 3.3 Deterministic risk enforcement

AI models and strategies can propose trades.

Only the deterministic risk system can authorize them.

The risk engine must be independent of AI-generated instructions and have absolute veto authority.

## 3.4 Reproducible decisions

Every trading decision must be traceable to the information actually available at that time.

Store:
- Market observations
- Feature values
- Feature availability timestamps
- Strategy version
- Model version
- AI feature versions
- Proposed trade
- Portfolio state
- Risk calculations
- Approval or rejection
- Execution simulation
- Resulting financial records

## 3.5 Financial precision

Financial accounting must never depend on casual floating-point calculations.

Use appropriate decimal arithmetic and explicit rounding rules for money, prices, quantities, tick sizes, and lot sizes.

Use `Decimal` and appropriate database numeric types for financial records and ledger operations.

Analytical libraries may use floating-point arrays internally, but conversion to executable quantities and recorded financial values must follow instrument-specific precision rules.

## 3.6 Controlled autonomy

Atlas can autonomously:
- Discover assets
- Evaluate market conditions
- Generate signals
- Select strategies
- Open paper positions
- Adjust permitted position-management parameters
- Close paper positions
- Train candidate models
- Evaluate performance
- Suspend strategies under predefined rules

Atlas cannot autonomously:
- Alter hard risk limits
- Give itself additional permissions
- Enable real-money execution
- Bypass the risk engine
- Modify historical accounting records
- Promote unvalidated models merely because of recent performance

# 4. FINAL SYSTEM ARCHITECTURE

Atlas consists of the following logical subsystems.

1. Market Data Engine
2. Dynamic Asset Discovery Engine
3. Market Intelligence Engine
4. Strategy Laboratory
5. AI and Machine Learning Engine
6. Portfolio Management Engine
7. Risk Management Engine
8. Paper Execution Engine
9. News and Event Intelligence
10. Backtesting and Learning Laboratory
11. Database and Accounting System
12. API, Dashboard, and Monitoring Layer

## 4.1 Architecture overview

```text
             MARKET DATA PROVIDERS
             Stocks / ETFs / Forex / Crypto
                         |
                         v
                MARKET DATA ENGINE
                         |
                Normalize / Validate
                         |
                         v
                  POSTGRESQL
                + TIME-SERIES DATA
                         |
            +------------+-------------+
            |                          |
            v                          v
      ASSET DISCOVERY            FEATURE PIPELINE
            |                          |
            v                          v
    MARKET INTELLIGENCE         STRATEGY LABORATORY
            |                          |
            +------------+-------------+
                         |
                         v
               AI DECISION ENGINE
              /                  \
       QUANTITATIVE ML       ASYNC NEWS FEATURES
              \                  /
               +----------------+
                         |
                         v
               PORTFOLIO MANAGER
                         |
                         v
             ATOMIC RISK RESERVATION
                         |
                         v
               DETERMINISTIC RISK
                         |
                         v
               PAPER EXECUTION ENGINE
                         |
                         v
                  PAPER BROKER
                         |
                         v
               DOUBLE-ENTRY LEDGER
                         |
                         v
                 POSTGRESQL
                         |
                         v
                 FASTAPI BACKEND
                         |
                         v
                  REACT DASHBOARD
```

Supporting services:

- Redis for caching and task coordination.
- Background workers for asynchronous processing.
- External APIs for approved data and AI services.
- Structured logging and monitoring.
- Docker Compose for local infrastructure.

### Event processing

Market data and system events should move through an event-driven processing pipeline.

Use a persistent event-outbox pattern for important internal events:

1. A state change is committed to PostgreSQL.
2. The corresponding event is recorded in an outbox within the same transaction.
3. A dispatcher publishes the event to the worker/task system.
4. Consumers process it idempotently.
5. Processing results and completion states are persisted.

This prevents the database from recording an order while silently losing the event that should have triggered subsequent processing.

Workers may retry failed tasks, but retries must not duplicate orders, journal entries, fills, or other financial events.

# 5. REPOSITORY STRUCTURE

Use a clean, modular repository.

```text
atlas-ai-trader/
|
|-- backend/
|   |-- alembic/
|   |
|   |-- app/
|       |-- main.py
|       |-- api/
|       |   |-- v1/
|       |-- core/
|       |   |-- config/
|       |   |-- logging/
|       |   |-- security/
|       |   |-- exceptions/
|       |   |-- clocks/
|       |
|       |-- database/
|       |   |-- models/
|       |   |-- repositories/
|       |   |-- sessions.py
|       |   |-- migrations/
|       |
|       |-- events/
|       |-- workers/
|       |-- data_engine/
|       |   |-- providers/
|       |   |-- adapters/
|       |   |-- normalization/
|       |   |-- validation/
|       |   |-- storage/
|       |
|       |-- asset_discovery/
|       |-- market_intelligence/
|       |-- feature_engine/
|       |
|       |-- strategies/
|       |   |-- momentum/
|       |   |-- trend_following/
|       |   |-- mean_reversion/
|       |   |-- breakout/
|       |   |-- volatility/
|       |   |-- event_driven/
|       |
|       |-- ai_engine/
|       |   |-- coordinator/
|       |   |-- llm/
|       |   |-- schemas/
|       |
|       |-- ml_engine/
|       |   |-- datasets/
|       |   |-- features/
|       |   |-- training/
|       |   |-- inference/
|       |   |-- validation/
|       |   |-- registry/
|       |
|       |-- portfolio/
|       |-- accounting/
|       |-- risk/
|       |-- execution/
|       |-- paper_broker/
|       |-- news/
|       |-- backtesting/
|       |-- learning/
|       |-- notifications/
|       |-- monitoring/
|       |-- tests/
|
|-- frontend/
|   |-- public/
|   |-- src/
|       |-- components/
|       |-- pages/
|       |-- layouts/
|       |-- charts/
|       |-- hooks/
|       |-- services/
|       |-- types/
|       |-- styles/
|
|-- docker/
|-- config/
|-- docs/
|-- scripts/
|-- data/
|-- logs/
|
|-- .env.example
|-- .gitignore
|-- docker-compose.yml
|-- pyproject.toml
|-- README.md
```

The exact structure may evolve, but system responsibilities and boundaries must remain clear.

# 6. TECHNOLOGY STACK

## Backend

- Python
- FastAPI
- Pydantic v2
- pydantic-settings
- SQLAlchemy 2.x
- Alembic
- pytest
- structlog

## Data and quantitative analysis

- Polars
- NumPy
- scikit-learn
- XGBoost
- Pandas compatibility where useful
- PyTorch only if later justified by measurable benefits

## Database and infrastructure

- PostgreSQL
- TimescaleDB extension when justified by time-series workload
- Redis
- Celery or another appropriately evaluated background-task system
- Docker
- Docker Compose

## Frontend

- React
- TypeScript
- Tailwind CSS
- TradingView Lightweight Charts
- Lucide icons

## AI services

- External LLM APIs for asynchronous news and event interpretation
- Locally trained quantitative ML models
- Structured JSON output validated with Pydantic
- Versioned prompts and model configurations

Do not select expensive vendors solely for convenience.

Start with the minimum necessary provider integrations and upgrade as Atlas's actual requirements justify the expense.

# 7. MARKET DATA ENGINE

The Market Data Engine is responsible for collecting, validating, normalizing, and storing all market information.

## 7.1 Provider adapter architecture

Each provider must have its own adapter.

Potential provider candidates:

| Provider | Potential use |
|---|---|
| Alpaca | Equity/ETF data and future paper-broker evaluation |
| Polygon.io | Equities, historical data, reference data |
| Twelve Data | Multiple financial markets |
| OANDA | Forex |
| Coinbase | Cryptocurrency |
| CCXT-supported exchanges | Cryptocurrency data integrations |
| Finnhub | News and company information |

These are candidates, not mandatory subscriptions.

Before selecting a provider, verify current coverage, data licensing, historical availability, API limits, costs, and data quality.

Atlas must use a standardized internal data model independent of the provider.

## 7.2 Market data types

Support:
- OHLCV candles
- Trades where available
- Bid/ask quotes
- Volume
- VWAP where available
- Historical prices
- Reference data
- Corporate actions
- Market calendars
- Relevant exchange metadata

## 7.3 Data normalization

Normalize:
- Timestamps
- Asset identifiers
- Price precision
- Quantity precision
- Currency
- Exchange identifiers
- Timeframe definitions
- Volume units

All internal timestamps must use UTC.

Store source-specific identifiers separately from Atlas's internal asset identifiers.

## 7.4 Data timestamps and availability

Every record should distinguish relevant timestamps:

- `event_time`: When the market event occurred.
- `published_at`: When a news article was published.
- `received_at`: When Atlas received it.
- `available_at`: When the record became available to Atlas for decision-making.
- `processed_at`: When Atlas completed processing it.

Historical simulations must only use records whose `available_at` timestamp is no later than the historical decision time.

This is mandatory for both market data and AI-generated features.

## 7.5 Data quality

Implement:
- Missing-data detection
- Duplicate detection
- Out-of-order event detection
- Stale-feed detection
- Invalid-price detection
- Abnormal candle validation
- Incomplete-candle identification
- Provider outage detection
- Reconnection logic
- Data provenance tracking

**Never silently replace missing data with zero.**

Missing volume must remain distinguishable from genuine zero volume.

If a strategy depends on a missing field, reject or suspend the associated signal.

Staleness thresholds must depend on:
- Market
- Provider
- Timeframe
- Exchange session
- Data type

A 60-second threshold may be appropriate for some feeds and inappropriate for others.

Candle finalization must be explicit. A strategy that requires completed candles must not accidentally analyze an unfinished candle as though it were final.

Keep raw provider data immutable where practical and maintain normalized records separately.

# 8. DYNAMIC ASSET DISCOVERY

Atlas must dynamically discover and qualify assets.

Use three layers:

### Discovery Pool

All instruments available through supported provider directories.

### Qualified Universe

Assets that pass Atlas's eligibility requirements.

### Active Watchlist

Qualified assets receiving additional analytical attention.

Evaluate:
- Average daily volume
- Dollar volume
- Liquidity
- Bid/ask spread
- Volatility
- Trading history
- Data reliability
- Market session
- Estimated costs
- Short availability
- Exchange support
- Asset characteristics

The ranking system should determine how much analytical attention an asset receives.

Ranking alone must never authorize a trade.

Exclude instruments that cannot be adequately priced, traded, or risk-managed.

Maintain historical universe membership with effective timestamps.

This prevents survivorship bias when evaluating past strategies.

Respect market-specific operating hours:
- Stocks and ETFs: exchange sessions and holidays.
- Forex: trading sessions, weekends, and rollover.
- Crypto: continuous operation, including weekends.

# 9. MARKET INTELLIGENCE ENGINE

The Market Intelligence Engine evaluates market-wide conditions.

## 9.1 Regime detection

Detect and estimate probabilities for:
- Bullish trend
- Bearish trend
- Sideways market
- Consolidation
- High volatility
- Low volatility
- Unstable/event-driven market

Potential inputs:
- Moving-average relationships
- ADX
- RSI
- ATR
- Realized volatility
- Volume
- Market breadth
- Cross-asset correlation
- Relevant macroeconomic features

Regime classifications should be probabilistic and evaluated historically.

Do not treat a single indicator as definitive proof of a regime.

## 9.2 Cross-market intelligence

Track relevant relationships between:
- Major indices
- Individual equities
- ETFs
- Sectors
- Forex pairs
- Cryptocurrency
- Relevant macroeconomic instruments

Correlation must be treated as time-varying.

The system must detect when previously correlated assets begin behaving differently.

Market intelligence may influence strategy ranking and exposure constraints, but a temporary intelligence timeout must not authorize a trade or bypass risk checks.

# 10. FEATURE ENGINEERING

Create a standardized feature pipeline shared by strategies, ML models, and historical testing.

Potential features include:

### Price and trend
- Returns across multiple horizons
- Moving averages
- EMA distance
- Trend slope
- Trend strength
- Price relative to historical range

### Momentum
- RSI
- Rate of change
- MACD
- Relative strength
- Momentum acceleration

### Volatility
- ATR
- Realized volatility
- Bollinger Band width
- Volatility percentiles
- Range expansion

### Volume
- Relative volume
- Volume trend
- Volume spikes
- VWAP distance
- Liquidity measurements

### Market structure
- Support and resistance estimates
- Breakout levels
- Recent highs and lows
- Range boundaries
- Consolidation measures

### Cross-asset
- Rolling correlation
- Relative performance
- Sector relationships
- Benchmark-relative performance

Features must be versioned and timestamped.

Feature calculations must not use future information.

Persist enough information to reproduce historical decisions.

# 11. STRATEGY LABORATORY

Develop multiple independent strategy families.

Every strategy must share a standardized interface and produce a structured proposal.

Strategies must not have direct access to execution APIs or permission to bypass risk checks.

## 11.1 Momentum Strategy

Evaluate:
- Price acceleration
- Relative strength
- Volume confirmation
- Momentum persistence
- Trend alignment

Support long and short proposals.

## 11.2 Trend-Following Strategy

Evaluate:
- EMA alignment
- Moving-average crossovers
- ADX
- Trend persistence
- Pullbacks
- Trailing exits

## 11.3 Mean-Reversion Strategy

Evaluate:
- RSI extremes
- Bollinger Bands
- Statistical deviations
- Volatility-adjusted distance from average
- Reversal confirmation

Do not blindly buy oversold assets or short overbought assets.

## 11.4 Breakout Strategy

Evaluate:
- Support and resistance
- Consolidation
- Volatility contraction
- Relative volume
- Breakout confirmation
- Failed breakouts

## 11.5 Volatility Strategy

Evaluate:
- ATR expansion
- Volatility compression
- Range expansion
- Regime changes
- Volatility-adjusted stops

## 11.6 Event-Driven Strategy

Evaluate:
- Earnings
- Economic releases
- Central bank decisions
- Inflation announcements
- Employment announcements
- Geopolitical developments
- Company news
- Industry news
- Cryptocurrency-specific developments

Initially use event intelligence primarily as an input to risk and strategy evaluation.

Avoid independently executing trades based on unverified LLM news interpretation.

## 11.7 Standardized strategy proposal

Each proposal must contain:

- Proposal ID
- Strategy ID
- Strategy version
- Asset ID
- Market
- Direction
- Timeframe
- Proposed entry
- Proposed stop
- Proposed target
- Expected holding period
- Signal strength
- Supporting features
- Required feature timestamps
- Expected transaction costs
- Expiration timestamp
- Creation timestamp
- Reasoning summary

Strategy modules should be independently testable, enabled, disabled, and compared.

# 12. AI AND MACHINE LEARNING ENGINE

Atlas must use a hybrid analytical architecture.

A single language model must never control the entire trading process.

## 12.1 Quantitative engine

Responsible for:
- Indicator calculations
- Feature calculations
- Statistical measurements
- Expected-return estimation
- Volatility measurement
- Correlation
- Numerical validation

## 12.2 Locally trained ML engine

Start with simple, measurable baselines.

Evaluate:
- Logistic regression
- Random Forest
- XGBoost

Additional architectures may be introduced when they demonstrate improved out-of-sample performance.

Potential functions:
- Probability estimation
- Regime classification
- Signal classification
- Return estimation
- Feature evaluation
- Strategy ranking

Do not assume model complexity improves results.

## 12.3 External LLM engine

External AI APIs may assist with:
- News classification
- Event extraction
- Sentiment analysis
- Research summaries
- Structured interpretation of conflicting information
- Analytical explanations

LLM output must use strict structured schemas.

LLMs cannot:
- Invent market prices
- Calculate authoritative position sizes
- Approve risk
- Submit orders
- Alter risk limits
- Access privileged execution controls

## 12.4 LLM asynchronous feature architecture

LLMs must not sit directly inside the critical trade-execution loop.

Instead:

1. News or event information enters Atlas.
2. The article is stored with its original publication and ingestion timestamps.
3. An asynchronous worker submits it for AI interpretation.
4. The AI returns a structured response.
5. Atlas validates the response against a strict schema.
6. The result is stored as an immutable feature record.
7. Its `available_at` timestamp is recorded.
8. Strategies may use it only after it becomes available.

This keeps trading decisions reproducible as far as possible and prevents a strategy from waiting indefinitely on a news API.

## 12.5 LLM auditability

Store:
- Provider
- Model identifier
- Model version where available
- Prompt version
- Input data identifiers or immutable input hash
- Structured input
- Structured output
- Validation result
- Request timestamp
- Response timestamp
- Feature availability timestamp
- Relevant source identifiers

Do not claim that an external LLM is perfectly deterministic.

The goal is traceability and replayability of stored outputs, not an assumption that future API calls will always return identical results.

Treat article text as untrusted input. Prompt injection in external content must not be able to change Atlas instructions or permissions.

## 12.6 Decision coordinator

The coordinator evaluates competing proposals using:
- Strategy output
- ML predictions
- Market regime
- Feature quality
- Expected return
- Expected costs
- Current portfolio state
- Existing exposure
- Correlation
- News features
- Model calibration

Return a structured decision:
- Approved for risk review
- Rejected
- Deferred
- Insufficient information

The decision coordinator cannot authorize execution directly.

The deterministic risk engine retains final authority.

# 13. PORTFOLIO MANAGEMENT ENGINE

Manage portfolio risk as a whole.

Responsibilities:
- Position tracking
- Available cash
- Equity
- Realized P/L
- Unrealized P/L
- Gross exposure
- Net exposure
- Asset-class exposure
- Sector exposure
- Correlation exposure
- Position sizing
- Portfolio concentration
- Pending order reservations

Support concurrent positions across different strategies and holding periods.

Account for the possibility that several individually acceptable trades create an excessive combined risk.

For example, several semiconductor stocks and a semiconductor ETF may represent one highly concentrated economic exposure.

Track:
- Long exposure
- Short exposure
- Gross exposure
- Net exposure
- Margin usage
- Available risk budget
- Reserved risk budget

Do not assume offsetting long and short positions eliminate all risk.

# 14. RISK MANAGEMENT ENGINE

This is the highest-priority operational subsystem.

It must be deterministic, independently testable, and impossible to bypass through strategy or AI code.

## 14.1 Initial risk settings

| Setting | Starting value |
|---|---:|
| Simulated equity | $100,000 |
| Risk per trade | 0.5% |
| Maximum open positions | 8 |
| Daily loss threshold | 2% |
| Weekly loss threshold | 5% |
| Portfolio drawdown threshold | 10% |
| Initial leverage | 1x |
| Maximum gross exposure | 100% equity |

The risk percentage is calculated from current equity unless an approved alternative is explicitly configured.

## 14.2 Position sizing

Initial calculation:

```text
Risk Budget = Current Equity × Risk Percentage

Raw Quantity =
    Risk Budget / Absolute(Entry Price - Stop Price)
```

This is only the initial sizing calculation.

The final size must also satisfy:
- Available capital
- Exposure limits
- Asset-specific contract specifications
- Tick size
- Lot size
- Liquidity
- Transaction costs
- Estimated slippage
- Correlated exposure
- Short availability
- Margin rules

Use instrument-specific sizing for stocks, forex, and crypto rather than assuming all instruments share the same contract structure.

## 14.3 Atomic risk reservations

This requirement is mandatory.

Multiple strategy workers may propose trades simultaneously.

Without transactional coordination, two workers could each see the same available capital and approve trades that collectively exceed portfolio limits.

Implement an atomic reservation workflow.

### Required workflow

1. Receive a trade proposal.
2. Start a database transaction.
3. Lock the relevant portfolio account or risk-state row using an appropriate database locking strategy.
4. Read current positions and pending reservations.
5. Calculate current equity and exposure.
6. Calculate proposed risk and notional exposure.
7. Check all hard limits.
8. Reject or reserve the proposed exposure.
9. Persist the reservation and approved order intent in the same transaction.
10. Record the audit event.
11. Commit.

The reservation must remain active until the associated order is resolved.

On a fill:
- Convert the filled portion of the reservation into actual position exposure.
- Retain reservations for remaining quantities.

On cancellation, expiration, or rejection:
- Release the appropriate remaining reservation.

A retry must not create another reservation for the same order intent.

Use:
- Unique idempotency keys
- Database uniqueness constraints
- Transactional locking
- Consistent lock ordering
- Explicit reservation statuses

Revalidate relevant risk conditions before simulated fills where appropriate.

## 14.4 Exposure limits

Support configurable:
- Per-trade risk
- Single-asset exposure
- Asset-class exposure
- Sector exposure
- Gross exposure
- Net exposure
- Correlated-cluster exposure
- Maximum open positions
- Available capital
- Short exposure
- Margin usage

Build the framework for these limits even if the initial configuration is conservative.

## 14.5 Circuit breakers

Implement:
- Daily loss circuit breaker
- Weekly loss circuit breaker
- Maximum drawdown circuit breaker
- Data-quality circuit breaker
- Provider-outage circuit breaker
- Execution-error circuit breaker
- Manual kill switch

Define daily and weekly loss from clearly specified starting-of-period equity, accounting for realized and unrealized P/L and costs.

When a loss circuit breaker activates, the default behavior should be:
- Block new entries.
- Preserve position monitoring.
- Permit risk-reducing actions.
- Record the event.
- Require the configured recovery or review process before resuming entries.

Do not automatically close every position unless a separately defined emergency liquidation policy requires it.

## 14.6 Short-selling safeguards

Account for:
- Borrow availability
- Borrow costs
- Margin requirements
- Short-sale proceeds
- Short-position liabilities
- Potentially unlimited theoretical loss

Do not treat a normal long-position sizing formula as sufficient for all short positions.

## 14.7 Risk configuration governance

All hard risk settings must be versioned.

Every change must record:
- Previous configuration
- New configuration
- Timestamp
- Authorized actor
- Reason

Autonomous models and strategies cannot modify hard limits.

# 15. ORDER AND EXECUTION SYSTEM

Atlas initially uses a simulated paper broker.

There must be no executable real-money brokerage path in this project.

## 15.1 Order lifecycle

Build an explicit order state machine.

Required conceptual states:

- PROPOSED
- PENDING_RISK
- REJECTED
- APPROVED_RESERVED
- OPEN
- PARTIALLY_FILLED
- FILLED
- CANCEL_PENDING
- CANCELLED
- EXPIRED
- FAILED

Define valid transitions centrally.

Invalid transitions must be rejected.

Every transition must be stored with its timestamp, actor or subsystem, and reason.

A canceled order must not release its reservation until its cancellation is confirmed within the simulation state machine.

## 15.2 Order support

Simulate:
- Market orders
- Limit orders
- Stop orders
- Stop-limit orders
- Take-profit orders
- Trailing stops

## 15.3 Execution realism

Model:
- Bid/ask spread
- Slippage
- Commissions
- Partial fills
- Liquidity limits
- Market gaps
- Execution delays
- Short borrow costs
- Forex rollover
- Crypto funding costs where relevant

Never assume every order fills instantly at the last displayed price.

## 15.4 Paper broker

The paper broker must:
- Receive approved order intents.
- Simulate eligible fills.
- Maintain order state.
- Produce fill events.
- Record execution costs.
- Update simulated positions through the portfolio/accounting process.
- Handle restarts without duplicating execution.

Use unique order identifiers and idempotency keys.

All simulated fills must originate from documented market-data and execution assumptions.

Clearly distinguish:
- Actual provider market data
- Simulated order submission
- Simulated fills
- Hypothetical backtest execution

Do not present simulated execution as real brokerage activity.

# 16. DOUBLE-ENTRY ACCOUNTING SYSTEM

Atlas must implement a proper accounting subsystem rather than relying solely on position-based P/L calculations.

## 16.1 Core requirements

Use immutable:
- Journal entries
- Journal lines
- Transaction identifiers
- Posting templates
- Reconciliation records

Every posted transaction must balance according to the defined accounting model.

Posted financial records must never be silently edited or deleted.

Corrections must use reversal entries or explicit correcting entries.

## 16.2 Accounting categories

Support relevant accounts for:
- Cash
- Long securities
- Short liabilities
- Receivables and payables where applicable
- Realized P/L
- Unrealized valuation changes
- Commissions
- Fees
- Borrow costs
- Funding costs
- Forex financing
- Corporate actions
- FX conversion and translation

## 16.3 Ledger structure

At minimum, design:

`journal_entries`
- Entry ID
- Event ID
- Account ID
- Effective timestamp
- Posting timestamp
- Entry type
- Source event
- Reversal reference
- Description

`journal_lines`
- Line ID
- Journal entry ID
- Account code
- Debit
- Credit
- Currency
- Functional-currency amount
- FX rate reference
- Asset reference where applicable

Also implement the required currency and asset quantity subledgers.

## 16.4 Multi-currency accounting

USD is the initial reporting currency.

Atlas must nonetheless be designed to support:
- Multiple cash currencies
- Forex positions
- Crypto assets
- Currency conversions
- Foreign-currency liabilities
- Timestamped FX valuations

Use explicit conversion rates and FX clearing/translation accounts where needed.

Document accounting conventions and test all posting templates.

Do not casually combine amounts from different currencies.

## 16.5 Accounting precision

- Use decimal types for stored financial amounts.
- Define instrument-specific price and quantity precision.
- Define rounding behavior.
- Store transaction costs explicitly.
- Preserve original fill prices and quantities.
- Never overwrite historical fills.

## 16.6 Portfolio reconciliation

At each accounting checkpoint, reconcile:
- Cash balances
- Asset quantities
- Position quantities
- Long asset valuations
- Short liabilities
- Accrued costs
- Realized P/L
- Unrealized valuation changes
- Reporting-currency equity

The simplified long-only formula of cash plus position value is not sufficient for all Atlas markets and short positions.

Use a formally defined accounting equation covering assets, liabilities, and equity.

Generate reconciliation errors as high-priority system health events.

The ledger is authoritative; displayed account summaries are derived or cached values.

# 17. NEWS AND EVENT INTELLIGENCE

Build a news collection and interpretation subsystem.

Monitor:
- Earnings
- Interest rates
- Federal Reserve announcements
- Inflation
- Employment
- Central bank decisions
- Industry events
- Company announcements
- Geopolitical developments
- Cryptocurrency ecosystem events

Extract:
- Headline
- Publisher
- Publication timestamp
- Ingestion timestamp
- Affected assets
- Event category
- Sentiment
- Potential impact
- Potential impact duration
- Source reliability
- Model identifier
- Prompt version
- Structured confidence information

Deduplicate content.

Distinguish publication time from ingestion and processing time.

Only make a news feature available after its validated output has been stored.

News is supporting information, not an automatic instruction to trade.

# 18. BACKTESTING ENGINE

Build an event-driven historical backtesting system.

Use as much shared strategy, risk, portfolio, and accounting logic as possible between backtesting and paper trading.

Support:
- Historical candles
- Historical quotes where available
- Multiple assets
- Multiple markets
- Multiple timeframes
- Long and short
- Position sizing
- Transaction costs
- Spreads
- Slippage
- Portfolio restrictions
- Market sessions
- Corporate actions
- Benchmarks

## 18.1 Look-ahead prevention

This requirement is critical.

Historical decisions must only access information available at their simulated decision time.

Use:
- Point-in-time data
- Explicit timestamps
- Feature availability timestamps
- Historical asset-universe membership
- Proper candle finalization
- Correct news availability times
- Versioned strategy and model inputs

Prevent:
- Look-ahead bias
- Survivorship bias
- Data leakage
- Overlapping-label contamination
- Unrealistic fills
- Future knowledge from corporate actions
- Future news information entering historical decisions

If both a stop and target are crossed in the same candle and finer data cannot determine which happened first, apply a conservative assumption such as stop-first.

## 18.2 Validation methods

Use chronological walk-forward validation as the primary evaluation method.

Support additional methods such as:
- Purged time-series validation
- Embargo periods
- Combinatorial Purged Cross-Validation

CPCV should be an additional robustness tool rather than an automatic substitute for realistic chronological testing.

Set purging and embargo rules based on:
- Strategy holding periods
- Label horizons
- Overlapping observations
- Feature construction
- Data dependencies

Maintain an untouched final test period that is not used for model or strategy selection.

## 18.3 Backtest records

Store:
- Strategy version
- Model version
- Data provider and data version
- Asset universe definition
- Date range
- Parameters
- Cost assumptions
- Risk settings
- Execution assumptions
- Result metrics
- Output references

Every backtest must be reproducible.

# 19. LEARNING AND MODEL GOVERNANCE

Atlas should continuously gather evidence and develop candidate improvements.

## 19.1 Learning pipeline

1. Collect historical and paper-trading outcomes.
2. Generate features and labels.
3. Build chronological datasets.
4. Train candidate models.
5. Validate candidates.
6. Run walk-forward tests.
7. Compare against existing models and simple baselines.
8. Run promising candidates in shadow mode.
9. Evaluate performance.
10. Submit candidates for approval before promotion.

## 19.2 Champion/challenger system

Maintain:
- Current approved model: Champion
- Candidate models: Challengers

Challengers may operate in shadow mode without execution authority.

Model promotion must require:
- Sufficient observations
- Out-of-sample evidence
- Robustness testing
- Transaction-cost analysis
- Risk review
- Version registration
- Explicit promotion approval

A good week or a small number of winning trades is not evidence of a reliable model.

## 19.3 Model performance measurements

Track:
- Total return
- Annualized return where appropriate
- Sharpe ratio
- Sortino ratio
- Maximum drawdown
- Win rate
- Average win
- Average loss
- Profit factor
- Expectancy
- Turnover
- Transaction costs
- Benchmark-relative performance

Evaluate separately by:
- Strategy
- Asset
- Asset class
- Timeframe
- Market regime
- Long versus short
- Volatility conditions

Models must be calibrated where their outputs represent probabilities.

## 19.4 Model versioning

Preserve:
- Training data versions
- Feature definitions
- Hyperparameters
- Model artifacts
- Evaluation results
- Code version
- Promotion history
- Model status

Never silently replace a deployed model.

A new model is a new version.

# 20. PAPER TRADING VALIDATION

Atlas starts with $100,000 in simulated capital.

It should operate autonomously only after the foundational risk and execution functionality has passed testing.

Use the following stages.

### Stage 1: Historical backtesting

Validate strategy logic, historical behavior, financial calculations, and realistic cost assumptions.

### Stage 2: Walk-forward validation

Test on chronological data not used during model fitting or tuning.

### Stage 3: Shadow mode

Process current data, generate trading decisions, and record hypothetical orders without actually opening even paper positions.

### Stage 4: Autonomous paper trading

Allow Atlas to:
- Discover assets
- Scan markets
- Generate proposals
- Evaluate opportunities
- Submit approved paper orders
- Manage positions
- Close positions
- Record all results

### Stage 5: Live-readiness evaluation

This is an evaluation stage only.

Live brokerage execution remains outside the scope of this project and must not be implemented or enabled automatically.

## 20.1 Proposed performance acceptance gates

These are preliminary evaluation requirements, not guarantees of profitability.

Suggested requirements include:
- A substantial number of completed trades, with 200 as an initial portfolio-wide evidence target.
- Adequate observations for each individual strategy being evaluated.
- Positive net expectancy after realistic costs.
- A proposed profit-factor threshold above 1.2 on the relevant out-of-sample evaluation.
- Risk-adjusted performance compared with an appropriate benchmark.
- Drawdown within approved limits.
- Evidence across multiple market regimes.
- No unexplained concentration of returns in one asset or a small number of trades.
- Accurate financial reconciliation.
- No unresolved critical risk-engine failures.
- Reliable data ingestion and recovery.
- Reproducible strategy and model decisions.

Do not allow an aggregate score to hide a failing strategy, market, or time period.

Insufficient evidence means the strategy remains unvalidated.

Paper trading duration must be determined by the quality and quantity of evidence, not an arbitrary deadline.

Paper trading does not perfectly reproduce real-world fills, market impact, liquidity, or brokerage behavior. Keep those limitations visible.

# 21. DATABASE ARCHITECTURE

Use PostgreSQL as the authoritative transactional database.

Use TimescaleDB where its time-series functionality materially improves performance and maintenance.

Do not force every table into a hypertable.

Potential tables include:

### Identity and instruments
- assets
- exchanges
- market_sessions
- asset_universe_history
- currencies
- instrument_specs

### Data
- raw_market_events
- candles
- quotes
- trades
- corporate_actions
- data_quality_events
- data_provider_status

### Analytical features
- feature_definitions
- feature_values
- market_regimes
- feature_snapshots

### Strategies and decisions
- strategies
- strategy_versions
- strategy_signals
- ai_decisions
- decision_evidence
- model_registry
- model_predictions
- model_training_runs

### News
- news_articles
- news_events
- news_features
- news_source_metadata

### Portfolio and accounting
- portfolios
- portfolio_accounts
- positions
- position_events
- account_snapshots
- journal_entries
- journal_lines
- account_balances
- asset_quantity_ledger
- reconciliations

### Risk
- risk_configurations
- risk_reservations
- risk_events
- circuit_breaker_events

### Execution
- orders
- order_events
- fills
- execution_costs
- paper_broker_events

### Research
- backtests
- backtest_results
- training_datasets
- model_evaluations
- model_promotions

### Operations
- audit_logs
- outbox_events
- worker_jobs
- system_health_events
- notification_events

Use:
- Primary keys
- Foreign keys
- Appropriate uniqueness constraints
- Query indexes
- UTC timestamps
- Decimal/numeric financial fields
- Migration-controlled schema changes

Every decision, order, fill, and journal entry must be traceable to its originating event and relevant versions.

Do not overwrite historical records.

# 22. DASHBOARD

Build a React and TypeScript web dashboard.

The interface should look and function like a professional trading research terminal.

## 22.1 Overview

Display:
- Total equity
- Daily P/L
- Weekly P/L
- Cumulative return
- Cash
- Open positions
- Available exposure
- Gross exposure
- Net exposure
- Current drawdown
- Current system status

## 22.2 Markets

Display:
- Tracked assets
- Asset discovery rankings
- Charts
- Timeframes
- Volatility
- Relative strength
- Market regimes
- Data freshness

## 22.3 AI Activity

Display:
- Recent proposals
- Approved proposals
- Rejected proposals
- Decision evidence
- Model predictions
- News analysis
- Rejection explanations

## 22.4 Portfolio

Display:
- Open positions
- Entry prices
- Current prices
- Quantity
- Unrealized P/L
- Realized P/L
- Stops
- Targets
- Exposure

## 22.5 Strategies

Display:
- Strategy status
- Version
- Performance
- Win rate
- Profit factor
- Expectancy
- Drawdown
- Regime-specific results

## 22.6 Performance

Display:
- Equity curve
- Drawdown
- Monthly returns
- Strategy comparisons
- Benchmark comparisons
- Risk-adjusted performance
- Trading costs

## 22.7 Backtesting

Allow:
- Strategy selection
- Asset selection
- Timeframe selection
- Historical date range
- Parameter configuration
- Test execution
- Result comparison

## 22.8 Risk Center

Display:
- Hard risk limits
- Current risk configuration
- Exposure
- Reserved exposure
- Daily and weekly loss
- Drawdown
- Circuit-breaker status
- Risk rejections
- Kill switch

## 22.9 Accounting

Display:
- Cash by currency
- Journal history
- Fees
- Realized P/L
- Unrealized P/L
- Equity reconciliation
- Accounting errors

## 22.10 System Health

Display:
- Provider status
- Database status
- Redis status
- Worker status
- Data freshness
- Model status
- API status
- Recent errors
- Recovery events

## 22.11 Settings

Provide configuration for approved, permitted settings.

Hard risk-limit changes must require explicit authorized user action and must produce an audit record.

The dashboard must never provide a hidden or automatic route into live order execution.

# 23. NOTIFICATIONS

Initially support dashboard notifications.

Design adapters for later Telegram and email integration.

Notify about:
- Opened paper positions
- Closed positions
- Important P/L events
- Rejected orders
- Risk circuit breakers
- Data outages
- Provider failures
- Model degradation
- Accounting discrepancies
- Worker failures
- Emergency pauses
- Successful system recovery

Avoid sending notifications for every minor market update.

# 24. SECURITY AND OPERATIONAL SAFETY

Mandatory requirements:

- No hardcoded secrets.
- Validate environment configuration at startup.
- Separate development and paper environments.
- Restrict provider API permissions.
- Do not request withdrawal permissions.
- Protect administrative endpoints.
- Use authentication for dashboard controls.
- Validate external API responses.
- Treat news and outside text as untrusted.
- Audit privileged configuration changes.
- Protect logs against accidental credential exposure.
- Implement backup and recovery procedures.
- Implement database migrations.
- Monitor worker health.
- Use least-privilege access.

## 24.1 Paper-only isolation

This requirement is non-negotiable.

During this project:
- No live broker integration.
- No real order-submission code.
- No real trading credentials.
- No automatic live mode.
- No AI-controlled account permissions.
- No hidden live-trading configuration switch.

The paper broker must be explicitly simulated and visibly labeled.

## 24.2 Emergency controls

Build:
- System pause
- Trading kill switch
- New-entry disable switch
- Strategy disable switch
- Data-feed disable switch
- Model disable switch

Distinguish between halting new entries and safely managing existing simulated positions.

# 25. TESTING STRATEGY

Testing must be built alongside functionality.

Do not wait until the project is complete.

## 25.1 Unit testing

Use pytest.

Test:
- Indicator calculations
- Feature calculations
- Strategy outputs
- Position sizing
- Exposure calculations
- Risk limits
- Reservation logic
- Order state transitions
- Journal posting templates
- Currency conversions
- P/L calculations
- Execution-cost calculations

Use deterministic test vectors and fixed clocks where possible.

## 25.2 Integration testing

Test:
- Provider adapter to normalization
- Market data to feature engine
- Feature engine to strategies
- Strategies to decision coordinator
- Decision coordinator to portfolio manager
- Portfolio manager to risk engine
- Risk approval to paper broker
- Fills to ledger
- Ledger to account summaries
- API to dashboard

## 25.3 Concurrency testing

This is especially important.

Simulate multiple workers submitting trades simultaneously.

Verify:
- No double-spending of risk budgets.
- No duplicate order approvals.
- No duplicate reservations.
- No incorrect exposure calculations.
- No missing ledger entries.
- No race-condition accounting errors.

## 25.4 Financial correctness testing

Verify:
- Journal transactions balance.
- Cash balances reconcile.
- Asset quantities reconcile.
- Position quantities reconcile.
- Fees are included.
- Short liabilities are included.
- Currency conversion is correct.
- Realized and unrealized P/L are separated.
- Equity reconciles to underlying accounts.
- Reversals preserve historical accounting records.

Use property-based testing where beneficial to test a wider range of quantities, prices, fees, and order sequences.

## 25.5 Failure testing

Simulate:
- Provider outages
- Missing data
- Stale data
- Duplicate events
- Out-of-order events
- API rate limits
- Database disconnections
- Redis failures
- Worker crashes
- Process restarts
- Partial fills
- Market gaps
- Unexpected slippage
- Conflicting signals
- Circuit-breaker activation
- Duplicate task delivery

Atlas must recover without duplicating financial activity.

# 26. LOCAL DEVELOPMENT AND CLOUD DEPLOYMENT

## 26.1 Local first

Develop through VS Code with Docker Compose.

Provide:
- One-command development startup
- Database startup
- Redis startup
- Backend startup
- Frontend startup
- Database migrations
- Automated test commands
- Paper-mode startup
- Safe shutdown
- Recovery procedures

Provide an `.env.example` documenting every configuration variable.

The local environment must be reproducible.

## 26.2 Cloud later

Cloud deployment only after local development and paper trading establish reliability.

Prepare for:
- Dockerized deployment
- Persistent database storage
- Monitoring
- Backups
- Secure secrets
- Automatic restart
- Health checks
- Worker recovery
- Provider reconnection
- Idempotent event processing

Do not add ongoing cloud expense prematurely.

# 27. IMPLEMENTATION ROADMAP

Build Atlas in carefully controlled phases.

A phase is complete only after its acceptance criteria pass.

## PHASE 1 — FOUNDATION

Establish the repository and infrastructure.

Tasks:
- Initialize Git.
- Create the repository structure.
- Configure `.gitignore`.
- Create `pyproject.toml`.
- Establish Python dependency management and a lock file.
- Establish the React/TypeScript frontend.
- Configure Docker Compose.
- Configure PostgreSQL.
- Configure Redis.
- Set up environment validation.
- Set up structured JSON logging with UTC timestamps.
- Initialize Alembic.
- Establish SQLAlchemy database connectivity.
- Create initial foundational schemas for assets, portfolio accounts, and audit logs.
- Implement FastAPI application skeleton.
- Implement `/health` and readiness endpoints.
- Create dashboard shell.
- Establish pytest and frontend testing structure.
- Create initial documentation.

Do not implement trading strategies or execution functionality during this phase.

**Acceptance criteria:**
- Repository starts locally.
- Database connects successfully.
- Redis connects successfully.
- Migrations run.
- Health and readiness endpoints function.
- Frontend loads.
- Environment configuration validates.
- Logging operates.
- Initial tests pass.

## PHASE 2 — MARKET DATA

Build:
- Provider interface
- First market-data integration
- Historical data retrieval
- Live feed handling where available
- Normalization
- Candle storage
- Data validation
- Data-quality events
- Provider health monitoring

Start with one market and a manageable number of assets.

**Acceptance criteria:** Reliable market data can be collected, normalized, stored, retrieved, and displayed.

## PHASE 3 — ASSET DISCOVERY AND MARKET INTELLIGENCE

Build:
- Asset directories
- Discovery pool
- Qualified universe
- Active watchlist
- Eligibility filters
- Market session calendars
- Initial indicators
- Feature definitions
- Regime detection
- Cross-asset correlation

**Acceptance criteria:** Atlas can discover eligible assets and produce timestamped, reproducible market features.

## PHASE 4 — STRATEGY LABORATORY

Build the six initial strategy families.

Establish:
- Strategy interfaces
- Proposal schemas
- Versioning
- Signal storage
- Strategy enable/disable controls
- Proposal expiration
- Independent strategy testing

**Acceptance criteria:** Strategies can independently generate structured proposals without submitting orders.

## PHASE 5 — AI AND ML ENGINE

Build:
- Quantitative feature pipeline
- Initial ML baselines
- Model training
- Chronological validation
- Model registry
- LLM provider adapter
- Asynchronous news analysis
- Structured output validation
- Decision coordinator

**Acceptance criteria:** Atlas can evaluate competing proposals with versioned, timestamped, reproducible inputs.

## PHASE 6 — PORTFOLIO, ACCOUNTING, AND RISK

Build:
- Portfolio models
- Double-entry ledger
- Multi-currency architecture
- Position tracking
- Cash accounting
- P/L calculations
- Exposure calculations
- Risk reservations
- Atomic approvals
- Circuit breakers
- Audit events

**Acceptance criteria:** All accounting transactions reconcile, concurrent risk tests pass, and no approved order bypasses the risk engine.

This phase requires especially strict review.

## PHASE 7 — PAPER EXECUTION

Build:
- Order state machine
- Simulated paper broker
- Market orders
- Limit orders
- Stop orders
- Take-profit
- Trailing stops
- Partial fills
- Spreads
- Slippage
- Fees
- Position monitoring

**Acceptance criteria:** Atlas can open, manage, and close paper positions without duplicate orders or incorrect accounting.

## PHASE 8 — BACKTESTING AND LEARNING

Build:
- Event-driven backtesting
- Historical portfolio simulation
- Transaction costs
- Walk-forward validation
- Purged validation
- CPCV where useful
- Benchmark comparisons
- Candidate model evaluation
- Champion/challenger workflow

**Acceptance criteria:** Results are reproducible, time-consistent, and based on realistic assumptions.

## PHASE 9 — COMPLETE DASHBOARD

Build all dashboard sections.

Ensure:
- Accurate live system state
- Portfolio visualization
- Strategy performance
- Risk center
- Accounting
- Backtesting
- AI decisions
- System health

**Acceptance criteria:** The complete paper-trading system can be monitored and controlled through the dashboard.

## PHASE 10 — AUTONOMOUS PAPER TRADING

Enable autonomous operation after required tests and checks.

Atlas may:
- Scan markets
- Discover assets
- Generate strategies
- Evaluate opportunities
- Approve permitted paper orders through the risk system
- Manage positions
- Close positions
- Record outcomes
- Evaluate performance

**Acceptance criteria:** Atlas runs continuously within the configured market schedules, maintains correct accounting, and safely handles failures.

## PHASE 11 — VALIDATION AND CLOUD READINESS

Evaluate:
- Paper-trading performance
- Risk adherence
- Accounting reconciliation
- Data quality
- Execution realism
- Strategy robustness
- Model performance
- Recovery reliability

Prepare cloud deployment only when justified.

No automatic live-trading transition is permitted.

# 28. CODING AI INSTRUCTIONS

The coding AI is expected to behave like a careful senior software engineer, not a code generator that blindly attempts to implement everything at once.

## Before implementation

1. Read this entire document.
2. Inspect the current workspace.
3. Identify whether any project files already exist.
4. Review the current architecture.
5. Identify unresolved technical decisions.
6. Produce a phase-specific implementation plan.

## During implementation

1. Work one phase at a time.
2. Preserve established module boundaries.
3. Use maintainable, typed, documented code.
4. Avoid unnecessary abstraction.
5. Do not fabricate market data or API responses.
6. Clearly identify fixtures, mocks, and simulations.
7. Write tests alongside features.
8. Run tests after meaningful changes.
9. Preserve migration history.
10. Do not silently change configuration defaults.
11. Do not implement out-of-scope features.
12. Protect financial calculations and transactional correctness.
13. Document architectural revisions.
14. Keep dependencies justified and manageable.

## At the completion of every phase

Report:

- Files created
- Files modified
- Architecture changes
- Features implemented
- Dependencies added
- Commands executed
- Tests performed
- Test results
- Known issues
- Remaining work
- How to run the application

Do not claim a feature works unless it has been implemented and tested.

Do not claim a phase is complete while critical acceptance criteria remain unmet.

When uncertain about a financial or architectural decision, stop and explain the issue rather than inventing a solution.

# 29. FINAL DEFINITION OF SUCCESS

Atlas AI Trader is considered successfully developed when it can:

1. Run reliably on a local machine.
2. Collect supported financial-market data.
3. Discover eligible assets dynamically.
4. Detect different market regimes.
5. Calculate reproducible features.
6. Generate proposals across multiple trading strategies.
7. Evaluate quantitative ML predictions.
8. Incorporate asynchronous AI news features.
9. Evaluate long and short opportunities.
10. Rank proposals against portfolio conditions.
11. Reserve risk atomically.
12. Reject unauthorized or excessive exposure.
13. Maintain a double-entry financial ledger.
14. Execute realistic paper trades.
15. Manage multiple positions automatically.
16. Reconcile cash, positions, costs, and equity.
17. Backtest strategies without look-ahead bias.
18. Train and evaluate candidate models.
19. Maintain model and strategy version histories.
20. Display operations through a functional dashboard.
21. Recover safely from interruptions.
22. Maintain a complete audit trail.
23. Operate autonomously in paper mode without compromising its restrictions.

# 30. THE CORE OBJECTIVE

Atlas is not supposed to be an AI that makes a large number of trades.

It is supposed to be an intelligent quantitative trading system that continuously searches for opportunities, evaluates them objectively, understands its own portfolio risk, and learns from measurable outcomes.

A complicated model is not automatically a good model.

A high win rate is not automatically a profitable strategy.

A profitable backtest is not proof of future performance.

An AI explanation is not evidence of a trading edge.

The system must earn confidence through historical validation, realistic simulation, financial correctness, and sustained performance across market conditions.

**Build Atlas to be selective, systematic, accountable, and evidence-driven.**

Its ability to reject a bad trade is just as important as its ability to identify a good one.

The coding AI must prioritize correctness over speed and preserve this philosophy throughout every implementation phase.