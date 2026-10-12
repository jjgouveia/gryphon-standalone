# Impact benchmark, run 20261004-091410-claude-haiku-4-5-20251001

## hard

| Braço | Execuções | precision | recall | f1 | file_recall | total_cost_usd | num_turns | wall_seconds | graph_tool_calls |
|---|---|---|---|---|---|---|---|---|---|
| baseline | 10 | 0.936 | 1.000 | 0.962 | 1.000 | 0.123 | 17.800 | 1990.460 | 0.000 |
| graph | 10 | 0.936 | 0.951 | 0.933 | 1.000 | 0.095 | 12.000 | 42.260 | 1.900 |

Diferença pareada por alvo (graph − baseline), 10 alvos, IC 95% bootstrap:

| Métrica | Diferença | IC 95% | Melhor | Pior |
|---|---|---|---|---|
| precision | +0.000 | [+0.000, +0.000] | 0 | 0 |
| recall | -0.049 | [-0.122, +0.000] | 0 | 2 |
| f1 | -0.029 | [-0.074, +0.000] | 0 | 2 |
| file_recall | +0.000 | [+0.000, +0.000] | 0 | 0 |
| total_cost_usd | -0.028 | [-0.081, +0.024] | 5 | 5 |
| num_turns | -5.800 | [-11.800, -0.500] | 2 | 5 |
| wall_seconds | -1948.200 | [-4854.020, -3.690] | 1 | 9 |

## easy

| Braço | Execuções | precision | recall | f1 | file_recall | total_cost_usd | num_turns | wall_seconds | graph_tool_calls |
|---|---|---|---|---|---|---|---|---|---|
| baseline | 4 | 1.000 | 1.000 | 1.000 | 1.000 | 0.047 | 8.000 | 2437.825 | 0.000 |
| graph | 4 | 1.000 | 1.000 | 1.000 | 1.000 | 0.077 | 11.500 | 35.175 | 1.500 |

Diferença pareada por alvo (graph − baseline), 4 alvos, IC 95% bootstrap:

| Métrica | Diferença | IC 95% | Melhor | Pior |
|---|---|---|---|---|
| precision | +0.000 | [+0.000, +0.000] | 0 | 0 |
| recall | +0.000 | [+0.000, +0.000] | 0 | 0 |
| f1 | +0.000 | [+0.000, +0.000] | 0 | 0 |
| file_recall | +0.000 | [+0.000, +0.000] | 0 | 0 |
| total_cost_usd | +0.030 | [+0.007, +0.049] | 3 | 1 |
| num_turns | +3.500 | [+2.000, +6.500] | 4 | 0 |
| wall_seconds | -2402.650 | [-7222.800, +12.900] | 2 | 2 |

## all

| Braço | Execuções | precision | recall | f1 | file_recall | total_cost_usd | num_turns | wall_seconds | graph_tool_calls |
|---|---|---|---|---|---|---|---|---|---|
| baseline | 14 | 0.954 | 1.000 | 0.973 | 1.000 | 0.101 | 15.000 | 2118.279 | 0.000 |
| graph | 14 | 0.954 | 0.965 | 0.952 | 1.000 | 0.090 | 11.857 | 40.236 | 1.786 |

Diferença pareada por alvo (graph − baseline), 14 alvos, IC 95% bootstrap:

| Métrica | Diferença | IC 95% | Melhor | Pior |
|---|---|---|---|---|
| precision | +0.000 | [+0.000, +0.000] | 0 | 0 |
| recall | -0.035 | [-0.097, +0.000] | 0 | 2 |
| f1 | -0.021 | [-0.058, +0.000] | 0 | 2 |
| file_recall | +0.000 | [+0.000, +0.000] | 0 | 0 |
| total_cost_usd | -0.011 | [-0.054, +0.028] | 8 | 6 |
| num_turns | -3.143 | [-8.071, +1.286] | 6 | 5 |
| wall_seconds | -2078.043 | [-4159.293, -7.836] | 3 | 11 |

## Por alvo

| Alvo | Grupo | Chamadas | Braço | Reportadas | VP | Precisão | Recall | Custo | Turnos |
|---|---|---|---|---|---|---|---|---|---|
| t01-formatCurrency | hard | 11 | baseline r0 | 11 | 11 | 1.00 | 1.00 | $0.19 | 16 |
| t01-formatCurrency | hard | 11 | graph r0 | 7 | 7 | 1.00 | 0.64 | $0.08 | 17 |
| t02-prefersReducedMotion | hard | 23 | baseline r0 | 23 | 23 | 1.00 | 1.00 | $0.14 | 27 |
| t02-prefersReducedMotion | hard | 23 | graph r0 | 20 | 20 | 1.00 | 0.87 | $0.05 | 7 |
| t03-BRL | hard | 37 | baseline r0 | 37 | 37 | 1.00 | 1.00 | $0.11 | 8 |
| t03-BRL | hard | 37 | graph r0 | 37 | 37 | 1.00 | 1.00 | $0.08 | 8 |
| t04-formatDate | hard | 6 | baseline r0 | 6 | 6 | 1.00 | 1.00 | $0.13 | 23 |
| t04-formatDate | hard | 6 | graph r0 | 6 | 6 | 1.00 | 1.00 | $0.14 | 12 |
| t05-formatBRL | hard | 10 | baseline r0 | 10 | 10 | 1.00 | 1.00 | $0.16 | 20 |
| t05-formatBRL | hard | 10 | graph r0 | 10 | 10 | 1.00 | 1.00 | $0.07 | 17 |
| t06-useOperationalListFilterQueryState | hard | 9 | baseline r0 | 13 | 9 | 0.69 | 1.00 | $0.25 | 30 |
| t06-useOperationalListFilterQueryState | hard | 9 | graph r0 | 13 | 9 | 0.69 | 1.00 | $0.07 | 8 |
| t07-uid | hard | 6 | baseline r0 | 6 | 6 | 1.00 | 1.00 | $0.06 | 17 |
| t07-uid | hard | 6 | graph r0 | 6 | 6 | 1.00 | 1.00 | $0.09 | 7 |
| t08-uid | hard | 7 | baseline r0 | 7 | 7 | 1.00 | 1.00 | $0.07 | 13 |
| t08-uid | hard | 7 | graph r0 | 7 | 7 | 1.00 | 1.00 | $0.09 | 13 |
| t09-canAccessGlobalSearch | hard | 8 | baseline r0 | 8 | 8 | 1.00 | 1.00 | $0.04 | 7 |
| t09-canAccessGlobalSearch | hard | 8 | graph r0 | 8 | 8 | 1.00 | 1.00 | $0.07 | 7 |
| t10-getPendenciasData | hard | 6 | baseline r0 | 9 | 6 | 0.67 | 1.00 | $0.08 | 17 |
| t10-getPendenciasData | hard | 6 | graph r0 | 9 | 6 | 0.67 | 1.00 | $0.21 | 24 |
| t11-sanitizeCrmRqlLiteral | easy | 10 | baseline r0 | 10 | 10 | 1.00 | 1.00 | $0.04 | 10 |
| t11-sanitizeCrmRqlLiteral | easy | 10 | graph r0 | 10 | 10 | 1.00 | 1.00 | $0.08 | 18 |
| t12-formatBreadcrumb | easy | 16 | baseline r0 | 16 | 16 | 1.00 | 1.00 | $0.05 | 7 |
| t12-formatBreadcrumb | easy | 16 | graph r0 | 16 | 16 | 1.00 | 1.00 | $0.11 | 9 |
| t13-checkIsVisitorRoute | easy | 12 | baseline r0 | 12 | 12 | 1.00 | 1.00 | $0.03 | 9 |
| t13-checkIsVisitorRoute | easy | 12 | graph r0 | 12 | 12 | 1.00 | 1.00 | $0.06 | 11 |
| t14-toHealthSegmentRatio | easy | 10 | baseline r0 | 10 | 10 | 1.00 | 1.00 | $0.06 | 6 |
| t14-toHealthSegmentRatio | easy | 10 | graph r0 | 10 | 10 | 1.00 | 1.00 | $0.05 | 8 |
