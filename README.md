# Lotofácil Analytics Pro v3.1

Aplicativo profissional em Python + Streamlit para análise da série histórica da Lotofácil.

## O que esta versão faz

- Lê histórico da Lotofácil por arquivo local, tabela colada ou download automático da CAIXA.
- Aceita XLSX, XLS, CSV, TXT, HTML, JSON e ZIP.
- Limpa a base procurando `Concurso`, `Data Sorteio` e `Bola1` até `Bola15`.
- Gera ranking de dezenas por frequência geral, semanal, mensal e anual.
- Calcula estatísticas de probabilidade: frequência esperada, desvio, z-score e p-valor aproximado.
- Aplica correção Benjamini-Hochberg nos p-valores aproximados.
- Calcula atraso/recência de cada dezena.
- Mede consistência por semanas, meses e anos.
- Calcula tendência por janela móvel.
- Calcula combinações recorrentes por semana, mês e geral.
- Calcula coocorrência de pares e matriz de calor.
- Faz teste de uniformidade por qui-quadrado.
- Faz backtest fora da amostra com modelos simples baseados em frequência, frequência recente e score composto.
- Exporta relatório completo em Excel com várias abas.

## Aviso estatístico

Este app faz estatística descritiva e validação exploratória. Ele não prevê sorteios. Loteria é evento aleatório; frequência passada não garante repetição futura.

## Fonte oficial

A página oficial da CAIXA informa a seção de download de resultados da Lotofácil:

https://loterias.caixa.gov.br/Paginas/Lotofacil.aspx

O modo automático usa endpoints internos do portal e pode falhar por bloqueio ou mudança da CAIXA. Para uso sério, baixe manualmente o histórico oficial e use `Importar arquivo local`.

## Como rodar

```bash
cd lotofacil_analytics_pro_v3_1
pip install -r requirements.txt
streamlit run streamlit_app.py
```

## Estrutura esperada da base

O arquivo deve ter pelo menos estas colunas:

```text
Concurso | Data Sorteio | Bola1 | Bola2 | ... | Bola15
```

Exemplo:

```text
1 | 29/09/2003 | 2 | 3 | 5 | 6 | 9 | 10 | 11 | 13 | 14 | 16 | 18 | 20 | 23 | 24 | 25
```

## Principais abas do Excel exportado

- `Resumo`
- `Metodologia`
- `Validacao_base`
- `Base_limpa`
- `Dezenas_modelagem`
- `Dezenas_semanal`
- `Dezenas_mensal`
- `Dezenas_anual`
- `Teste_uniformidade`
- `Tendencia_rolling`
- `Combos_semanal`
- `Combos_mensal`
- `Combos_geral`
- `Pares_coocorrencia`
- `Matriz_coocorrencia`
- `Backtest_resumo`
- `Backtest_detalhado`
- `Probabilidades`

## Interpretação prática

O ponto mais importante é o backtest. Se os modelos ficam perto da média teórica de 9 acertos, não há evidência prática de vantagem sobre seleção aleatória de 15 números.


## Correção v3.1

- Corrigido `KeyError: periodo` no cálculo de frequência geral das dezenas.
- A função `number_frequency_period(..., "geral")` agora cria a coluna `periodo` também na base usada para contar concursos.
