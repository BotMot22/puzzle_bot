#!/bin/bash
# Watch for state changes: resolutions, new trades, redeems
STATE="/root/polymarket_quant/data/expiry_state.json"
LAST_WINS=0
LAST_LOSSES=0
LAST_TRADES=0
LAST_RESOLVED=0

while true; do
    WINS=$(python3 -c "import json; d=json.load(open('$STATE')); print(d.get('wins',0))")
    LOSSES=$(python3 -c "import json; d=json.load(open('$STATE')); print(d.get('losses',0))")
    TRADES=$(python3 -c "import json; d=json.load(open('$STATE')); print(d.get('trades',0))")
    RESOLVED=$(python3 -c "import json; d=json.load(open('$STATE')); print(len(d.get('resolved_trades',[])))")
    PNL=$(python3 -c "import json; d=json.load(open('$STATE')); print(d.get('pnl',0))")
    BANKROLL=$(python3 -c "import json; d=json.load(open('$STATE')); print(d.get('bankroll',0))")
    PENDING=$(python3 -c "import json; d=json.load(open('$STATE')); print(len(d.get('pending',[])))")
    NOW=$(date -u '+%H:%M:%S UTC')

    if [ "$WINS" != "$LAST_WINS" ] || [ "$LOSSES" != "$LAST_LOSSES" ]; then
        echo "[$NOW] RESOLUTION: ${WINS}W-${LOSSES}L | PnL: \$${PNL} | Bankroll: \$${BANKROLL} | Pending: ${PENDING}"
        # Dump recently resolved
        python3 -c "
import json
d=json.load(open('$STATE'))
for t in d.get('resolved_trades',[])[-5:]:
    mark = 'WIN' if t.get('won') else 'LOSS'
    print(f\"  {mark}: {t.get('question','?')[:60]} | {t.get('outcome','?')} | PnL: \${t.get('pnl',0):+.2f}\")
"
    fi

    if [ "$TRADES" != "$LAST_TRADES" ]; then
        echo "[$NOW] NEW TRADE: total=${TRADES} | Bankroll: \$${BANKROLL} | Pending: ${PENDING}"
        # Show latest pending trade
        python3 -c "
import json
d=json.load(open('$STATE'))
p = d.get('pending',[])
if p:
    t = p[-1]
    print(f\"  PLACED: {t.get('question','?')[:60]} | {t.get('outcome','?')} @ \${t.get('clob_ask',0):.2f} | \${t.get('bet_size',0):.2f}\")
"
    fi

    LAST_WINS=$WINS
    LAST_LOSSES=$LOSSES
    LAST_TRADES=$TRADES
    LAST_RESOLVED=$RESOLVED

    sleep 30
done
