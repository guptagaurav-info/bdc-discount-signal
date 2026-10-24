# How this project works

Two diagrams:
1. **Pipeline**: every step, its source file, and the gates that stop bad data.
2. **The signal and the decision**: what the discount says and how a valuation committee should use it.

Colour key: **blue** = external data or market input · **lavender** = code step · **red** = check, gate or decision · **green** = good outcome · **yellow** = caution · **grey** = output

## 1. Pipeline (what `make all` runs)

```mermaid
flowchart TD
    A["SOURCE: BDC daily closes, dividend and split events<br/>mirror: huggingface.co/datasets/defeatbeta/yahoo-finance-data"]
    B["SOURCE: quarterly balance sheets + company profiles<br/>(common equity, shares outstanding, business summary)"]
    C["SOURCE: 3-month Treasury bill yield"]
    I{"ingest.py<br/>sha256 matches config?"}
    A --> I
    B --> I
    C --> I
    I -- "no" --> X["Stop: source changed"]
    I -- "yes" --> L

    subgraph L["Build the panel · pipeline.py"]
      L1["Universe: profiles describing a business development company,<br/>minus notes, preferreds, ETNs"]
      L2["NAV per share = equity / shares,<br/>shares restated for later splits"]
      L3["Price / NAV at the first close<br/>50 days after quarter-end (NAV public)"]
      L4["Outcome: NAV change over the next 4 quarters<br/>Control: NAV change over the previous 4"]
    end

    subgraph G["Gates"]
      G1{"No price before listing<br/>(close must be within 7 days)"}
      G2{"Data screen: drop any year spanning a<br/>quarter-on-quarter NAV/share jump above 25%"}
      G3{"Look-ahead unit tests: future prices and NAVs<br/>cannot change today's price / NAV"}
    end

    subgraph T["Tests"]
      T1["Spearman within each quarter"]
      T2["Fixed-effects regression,<br/>with and without trailing marks"]
      T3["Halves · 75- and 90-day lags · buckets · 0.85× flag"]
    end

    O["reports/metrics.json · panel.csv · latest_snapshot.csv · figures/*.png"]
    L --> G --> T --> O

    classDef src fill:#dbe8ff,stroke:#1f5fbf,color:#0b1f44
    classDef step fill:#ece8ff,stroke:#6b5fbf,color:#1d1640
    classDef gate fill:#ffe0e0,stroke:#c03030,color:#400000
    classDef out fill:#eeeeee,stroke:#777,color:#222
    class A,B,C src
    class L1,L2,L3,L4,T1,T2,T3 step
    class I,X,G1,G2,G3 gate
    class O out
```

![Pipeline](../reports/figures/flow-1.png)

## 2. The signal and the decision

```mermaid
flowchart LR
    M["Manager marks private loans<br/>once a quarter (NAV)"] --> N["NAV published<br/>40–60 days later"]
    S["Market prices the BDC<br/>every day"] --> D{"Price / NAV<br/>vs peers that quarter"}
    N --> D
    D -- "below 0.85×" --> R1["43% marked down >5%<br/>within a year<br/>avg NAV change −5.4%"]
    D -- "1.00× and above" --> R2["24% marked down >5%<br/>avg NAV change −2.2%"]
    R1 --> V{"Valuation committee<br/>(Rule 2a-5 oversight)"}
    R2 --> V
    V --> V1["Use the discount as a challenge prompt:<br/>re-test the marks on the names a deep-discount<br/>peer also holds"]
    V --> V2["Do not mark to the share price:<br/>it also prices fees, leverage, liquidity"]
    V1 --> USD["Illustration: 10 points of relative discount<br/>≈ $11.4M of later markdown per $1bn"]
    classDef src fill:#dbe8ff,stroke:#1f5fbf,color:#0b1f44
    classDef gate fill:#ffe0e0,stroke:#c03030,color:#400000
    classDef ok fill:#e3f3e3,stroke:#3a8a3a,color:#103010
    classDef rule fill:#fff4cc,stroke:#b38f00,color:#3d3000
    class M,N,S src
    class D,V gate
    class R2,V1 ok
    class R1,V2,USD rule
```

![Decision](../reports/figures/flow-2.png)
