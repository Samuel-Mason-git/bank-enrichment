# Your data

[← Back to the README](../README.md)

## Every transaction is kept

A webhook for a transaction already stored is ignored. Some merchants (e.g. Aldi, Lidl) only
fire when the transaction settles, days after the purchase, and that must not become a second
transaction.

## Why store context separately from labels

Labels change. The categories you care about today won't be the same in five 
years. By storing your raw context separately from any classification layer, 
you can re-run labelling at any point using a new taxonomy — without losing 
any of your original data.

Feed your enriched transaction history to an LLM with a completely different 
set of categories and it will reclassify everything correctly, because the 
context sentences tell it exactly what each transaction was.

## What you end up with

A Postgres database (schema `bank_enrichment`) of every transaction:

- The raw bank data exactly as Monzo sent it (amount, merchant, timestamp, counterparty — the full payload)
- Your one-sentence human context (what it actually was), and every answer before it
- Every label it has had — LLM, quick-tap, rule or your own — with when and by what
- A `transactions` view that puts the latest of each together, one row per transaction

Every time is stored in UTC and shown in London time, so a day, a week or a month is
counted the way you live it: a payment at 00:30 in summer belongs to that day, not the one before.

