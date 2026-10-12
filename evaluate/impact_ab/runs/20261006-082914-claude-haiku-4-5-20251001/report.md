# Impact benchmark, run 20261006-082914-claude-haiku-4-5-20251001

## hard

| Braço | Execuções | precision | recall | f1 | file_recall | total_cost_usd | num_turns | wall_seconds | graph_tool_calls |
|---|---|---|---|---|---|---|---|---|---|
| baseline | 10 | 0.936 | 1.000 | 0.962 | 1.000 | 0.123 | 18.100 | 57.750 | 0.000 |
| graph | 10 | 0.969 | 1.000 | 0.982 | 1.000 | 0.079 | 14.400 | 700.470 | 1.700 |

Diferença pareada por alvo (graph − baseline), 10 alvos, IC 95% bootstrap:

| Métrica | Diferença | IC 95% | Melhor | Pior |
|---|---|---|---|---|
| precision | +0.033 | [+0.000, +0.100] | 1 | 0 |
| recall | +0.000 | [+0.000, +0.000] | 0 | 0 |
| f1 | +0.020 | [+0.000, +0.060] | 1 | 0 |
| file_recall | +0.000 | [+0.000, +0.000] | 0 | 0 |
| total_cost_usd | -0.044 | [-0.093, -0.002] | 2 | 8 |
| num_turns | -3.700 | [-8.300, +2.200] | 2 | 8 |
| wall_seconds | +642.720 | [-10.200, +1306.490] | 4 | 6 |

## easy

| Braço | Execuções | precision | recall | f1 | file_recall | total_cost_usd | num_turns | wall_seconds | graph_tool_calls |
|---|---|---|---|---|---|---|---|---|---|
| baseline | 4 | 1.000 | 1.000 | 1.000 | 1.000 | 0.056 | 8.750 | 36.125 | 0.000 |
| graph | 4 | 1.000 | 1.000 | 1.000 | 1.000 | 0.041 | 7.500 | 26.050 | 1.000 |

Diferença pareada por alvo (graph − baseline), 4 alvos, IC 95% bootstrap:

| Métrica | Diferença | IC 95% | Melhor | Pior |
|---|---|---|---|---|
| precision | +0.000 | [+0.000, +0.000] | 0 | 0 |
| recall | +0.000 | [+0.000, +0.000] | 0 | 0 |
| f1 | +0.000 | [+0.000, +0.000] | 0 | 0 |
| file_recall | +0.000 | [+0.000, +0.000] | 0 | 0 |
| total_cost_usd | -0.014 | [-0.037, +0.008] | 2 | 2 |
| num_turns | -1.250 | [-3.250, +1.000] | 1 | 3 |
| wall_seconds | -10.075 | [-16.775, +1.100] | 1 | 3 |

## all

| Braço | Execuções | precision | recall | f1 | file_recall | total_cost_usd | num_turns | wall_seconds | graph_tool_calls |
|---|---|---|---|---|---|---|---|---|---|
| baseline | 14 | 0.954 | 1.000 | 0.973 | 1.000 | 0.104 | 15.429 | 51.571 | 0.000 |
| graph | 14 | 0.978 | 1.000 | 0.987 | 1.000 | 0.068 | 12.429 | 507.779 | 1.500 |

Diferença pareada por alvo (graph − baseline), 14 alvos, IC 95% bootstrap:

| Métrica | Diferença | IC 95% | Melhor | Pior |
|---|---|---|---|---|
| precision | +0.024 | [+0.000, +0.071] | 1 | 0 |
| recall | +0.000 | [+0.000, +0.000] | 0 | 0 |
| f1 | +0.014 | [+0.000, +0.043] | 1 | 0 |
| file_recall | +0.000 | [+0.000, +0.000] | 0 | 0 |
| total_cost_usd | -0.035 | [-0.073, -0.006] | 4 | 10 |
| num_turns | -3.000 | [-6.571, +1.286] | 3 | 11 |
| wall_seconds | +456.207 | [-12.736, +934.771] | 5 | 9 |

## Por alvo

| Alvo | Grupo | Chamadas | Braço | Reportadas | VP | Precisão | Recall | Custo | Turnos |
|---|---|---|---|---|---|---|---|---|---|
| t01-formatCurrency | hard | 11 | baseline r0 | 11 | 11 | 1.00 | 1.00 | $0.16 | 15 |
| t01-formatCurrency | hard | 11 | graph r0 | 11 | 11 | 1.00 | 1.00 | $0.08 | 14 |
| t02-prefersReducedMotion | hard | 23 | baseline r0 | 23 | 23 | 1.00 | 1.00 | $0.13 | 38 |
| t02-prefersReducedMotion | hard | 23 | graph r0 | 23 | 23 | 1.00 | 1.00 | $0.08 | 27 |
| t03-BRL | hard | 37 | baseline r0 | 37 | 37 | 1.00 | 1.00 | $0.09 | 12 |
| t03-BRL | hard | 37 | graph r0 | 37 | 37 | 1.00 | 1.00 | $0.05 | 7 |
| t04-formatDate | hard | 6 | baseline r0 | 6 | 6 | 1.00 | 1.00 | $0.14 | 17 |
| t04-formatDate | hard | 6 | graph r0 | 6 | 6 | 1.00 | 1.00 | $0.18 | 36 |
| t05-formatBRL | hard | 10 | baseline r0 | 10 | 10 | 1.00 | 1.00 | $0.27 | 18 |
| t05-formatBRL | hard | 10 | graph r0 | 10 | 10 | 1.00 | 1.00 | $0.05 | 9 |
| t06-useOperationalListFilterQueryState | hard | 9 | baseline r0 | 13 | 9 | 0.69 | 1.00 | $0.07 | 17 |
| t06-useOperationalListFilterQueryState | hard | 9 | graph r0 | 13 | 9 | 0.69 | 1.00 | $0.12 | 19 |
| t07-uid | hard | 6 | baseline r0 | 6 | 6 | 1.00 | 1.00 | $0.11 | 22 |
| t07-uid | hard | 6 | graph r0 | 6 | 6 | 1.00 | 1.00 | $0.10 | 8 |
| t08-uid | hard | 7 | baseline r0 | 7 | 7 | 1.00 | 1.00 | $0.09 | 16 |
| t08-uid | hard | 7 | graph r0 | 7 | 7 | 1.00 | 1.00 | $0.04 | 9 |
| t09-canAccessGlobalSearch | hard | 8 | baseline r0 | 8 | 8 | 1.00 | 1.00 | $0.07 | 11 |
| t09-canAccessGlobalSearch | hard | 8 | graph r0 | 8 | 8 | 1.00 | 1.00 | $0.02 | 5 |
| t10-getPendenciasData | hard | 6 | baseline r0 | 9 | 6 | 0.67 | 1.00 | $0.08 | 15 |
| t10-getPendenciasData | hard | 6 | graph r0 | 6 | 6 | 1.00 | 1.00 | $0.07 | 10 |
| t11-sanitizeCrmRqlLiteral | easy | 10 | baseline r0 | 10 | 10 | 1.00 | 1.00 | $0.05 | 10 |
| t11-sanitizeCrmRqlLiteral | easy | 10 | graph r0 | 10 | 10 | 1.00 | 1.00 | $0.06 | 8 |
| t12-formatBreadcrumb | easy | 16 | baseline r0 | 16 | 16 | 1.00 | 1.00 | $0.07 | 8 |
| t12-formatBreadcrumb | easy | 16 | graph r0 | 16 | 16 | 1.00 | 1.00 | $0.03 | 7 |
| t13-checkIsVisitorRoute | easy | 12 | baseline r0 | 12 | 12 | 1.00 | 1.00 | $0.04 | 8 |
| t13-checkIsVisitorRoute | easy | 12 | graph r0 | 12 | 12 | 1.00 | 1.00 | $0.04 | 10 |
| t14-toHealthSegmentRatio | easy | 10 | baseline r0 | 10 | 10 | 1.00 | 1.00 | $0.06 | 9 |
| t14-toHealthSegmentRatio | easy | 10 | graph r0 | 10 | 10 | 1.00 | 1.00 | $0.03 | 5 |
