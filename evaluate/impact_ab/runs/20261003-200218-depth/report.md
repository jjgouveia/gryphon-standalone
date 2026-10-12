# Transitive impact benchmark, run 20261003-200218-depth

| Braço | Execuções | precision | recall | level1_recall | level2_recall | f1 | total_cost_usd | num_turns | wall_seconds | graph_tool_calls |
|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 16 | 0.950 | 0.981 | 0.992 | 0.968 | 0.963 | 0.146 | 17.562 | 41.506 | 0.000 |
| graph | 16 | 0.943 | 0.954 | 0.984 | 0.925 | 0.945 | 0.139 | 15.375 | 39.800 | 1.438 |

Diferença pareada por alvo (graph − baseline), 8 alvos, IC 95% bootstrap:

| Métrica | Diferença | IC 95% | Melhor | Pior |
|---|---|---|---|---|
| precision | -0.007 | [-0.020, +0.000] | 0 | 1 |
| recall | -0.027 | [-0.063, +0.000] | 0 | 2 |
| level1_recall | -0.008 | [-0.023, +0.000] | 0 | 1 |
| level2_recall | -0.043 | [-0.105, +0.000] | 0 | 2 |
| f1 | -0.018 | [-0.043, +0.000] | 0 | 2 |
| total_cost_usd | -0.007 | [-0.026, +0.015] | 3 | 5 |
| num_turns | -2.188 | [-5.500, +1.250] | 3 | 5 |
| wall_seconds | -1.706 | [-6.256, +3.094] | 3 | 5 |

## Por alvo

| Alvo | Verdade | Braço | Reportadas | VP | Precisão | Recall | Custo | Turnos |
|---|---|---|---|---|---|---|---|---|
| d01-getDocsFromEsteira | 19 | baseline r0 | 19 | 16 | 0.84 | 0.84 | $0.21 | 19 |
| d01-getDocsFromEsteira | 19 | baseline r1 | 20 | 19 | 0.95 | 1.00 | $0.20 | 26 |
| d01-getDocsFromEsteira | 19 | graph r0 | 19 | 16 | 0.84 | 0.84 | $0.17 | 20 |
| d01-getDocsFromEsteira | 19 | graph r1 | 19 | 16 | 0.84 | 0.84 | $0.17 | 10 |
| d02-captureCedenteFormEvent | 15 | baseline r0 | 15 | 15 | 1.00 | 1.00 | $0.17 | 19 |
| d02-captureCedenteFormEvent | 15 | baseline r1 | 15 | 15 | 1.00 | 1.00 | $0.17 | 19 |
| d02-captureCedenteFormEvent | 15 | graph r0 | 11 | 11 | 1.00 | 0.73 | $0.14 | 18 |
| d02-captureCedenteFormEvent | 15 | graph r1 | 15 | 15 | 1.00 | 1.00 | $0.17 | 17 |
| d03-PresenceAvatarStack | 9 | baseline r0 | 9 | 9 | 1.00 | 1.00 | $0.08 | 8 |
| d03-PresenceAvatarStack | 9 | baseline r1 | 9 | 9 | 1.00 | 1.00 | $0.07 | 13 |
| d03-PresenceAvatarStack | 9 | graph r0 | 9 | 9 | 1.00 | 1.00 | $0.10 | 12 |
| d03-PresenceAvatarStack | 9 | graph r1 | 9 | 9 | 1.00 | 1.00 | $0.07 | 7 |
| d04-useSmartBlockHistoricalSuggestionsQuery | 11 | baseline r0 | 11 | 11 | 1.00 | 1.00 | $0.14 | 20 |
| d04-useSmartBlockHistoricalSuggestionsQuery | 11 | baseline r1 | 11 | 11 | 1.00 | 1.00 | $0.15 | 21 |
| d04-useSmartBlockHistoricalSuggestionsQuery | 11 | graph r0 | 11 | 11 | 1.00 | 1.00 | $0.15 | 23 |
| d04-useSmartBlockHistoricalSuggestionsQuery | 11 | graph r1 | 11 | 11 | 1.00 | 1.00 | $0.16 | 22 |
| d05-usePdfViewer | 14 | baseline r0 | 14 | 14 | 1.00 | 1.00 | $0.12 | 9 |
| d05-usePdfViewer | 14 | baseline r1 | 14 | 14 | 1.00 | 1.00 | $0.11 | 9 |
| d05-usePdfViewer | 14 | graph r0 | 14 | 14 | 1.00 | 1.00 | $0.19 | 16 |
| d05-usePdfViewer | 14 | graph r1 | 14 | 14 | 1.00 | 1.00 | $0.15 | 14 |
| d06-JuridicoCommonDialog | 15 | baseline r0 | 15 | 15 | 1.00 | 1.00 | $0.19 | 28 |
| d06-JuridicoCommonDialog | 15 | baseline r1 | 15 | 15 | 1.00 | 1.00 | $0.18 | 25 |
| d06-JuridicoCommonDialog | 15 | graph r0 | 15 | 15 | 1.00 | 1.00 | $0.15 | 20 |
| d06-JuridicoCommonDialog | 15 | graph r1 | 15 | 15 | 1.00 | 1.00 | $0.12 | 18 |
| d07-paginatedTableDisplayMessage | 13 | baseline r0 | 13 | 12 | 0.92 | 0.92 | $0.11 | 17 |
| d07-paginatedTableDisplayMessage | 13 | baseline r1 | 13 | 12 | 0.92 | 0.92 | $0.12 | 11 |
| d07-paginatedTableDisplayMessage | 13 | graph r0 | 13 | 12 | 0.92 | 0.92 | $0.12 | 19 |
| d07-paginatedTableDisplayMessage | 13 | graph r1 | 13 | 12 | 0.92 | 0.92 | $0.10 | 10 |
| d08-normalizeLiquidacaoListFilterFromUrl | 14 | baseline r0 | 18 | 14 | 0.78 | 1.00 | $0.15 | 16 |
| d08-normalizeLiquidacaoListFilterFromUrl | 14 | baseline r1 | 18 | 14 | 0.78 | 1.00 | $0.18 | 21 |
| d08-normalizeLiquidacaoListFilterFromUrl | 14 | graph r0 | 18 | 14 | 0.78 | 1.00 | $0.14 | 11 |
| d08-normalizeLiquidacaoListFilterFromUrl | 14 | graph r1 | 18 | 14 | 0.78 | 1.00 | $0.14 | 9 |
