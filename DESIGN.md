# Design

## Overview

The system has two halves. A **consumer** reads company updates from the queue, cleans them, and keeps one
up-to-date record per company in Postgres, together with an embedding of what the company does. An **API** answers
"which companies are like this one?" by finding the nearest embeddings in the same database.

The consumer does all the heavy work when data arrives, so the API only has to look things up when a page loads.
Processing the whole feed produces exactly 2,000 companies, and processing it a second time changes nothing.

Most of my time went into the data. It is the fuel for the whole system: if it's outdated or wrong, the
recommendations are wrong too, and a user can miss a company that matters to them.

## Decisions

| Decision | What it means | Why |
|---|---|---|
| **Reject data that can't be trusted** | 32 messages are skipped: broken JSON, missing company ID or timestamp, and placeholders named "No Reg Sdn. Bhd.", "Unknown" or "X". Each is logged and kept | Guessing a fix could overwrite a real company with junk. I checked that no company is lost |
| **Clean data that is only messy** | Company IDs in lowercase or with spaces, five timestamp formats and mixed-case state names are standardised | So the same company is always recognised as the same company |
| **The newest information wins** | Updates are compared by when the change happened, not when it arrived. Partial updates change only the fields they contain | 66 companies receive an older update after a newer one. Old data must never overwrite new, and a name change mustn't erase the description |
| **Make duplicates harmless** | Applying the same update twice has no extra effect | The queue can deliver a message more than once, and the feed has 68 exact copies |
| **Work within a queue I don't own** | Acknowledge only after saving; retry a failing message once, then move on | The registry team controls the queue, which rules out RabbitMQ's built-in failed-message handling. One bad message mustn't block every other company |
| **Keep a copy of every message** | Every original message is stored | RabbitMQ deletes messages once delivered. The copy gives a full history for debugging or rebuilding (e.g. after changing the model) |
| **One database for details and embeddings** | Postgres with pgvector holds both, saved together | They can never disagree, and filtering happens in the same search. A separate vector database would add complexity and nothing else at this size |
| **Mark struck-off companies, don't delete them** | They're excluded from recommendations | Their history is kept |
| **A simple, read-only API** | One endpoint returns the most similar companies with name, industry, state and description. Accepts IDs in any case; clear errors for invalid or unknown IDs | Gives the portal card what it needs, and the description shows why each company was recommended |
| **Choose the embedding model by measuring** | A multilingual model instead of the suggested English one | A third of companies describe their business in Malay. See the deep dive |

## Assumptions

Where the brief didn't say, I assumed the following:

- **Timestamps without a timezone are Malaysian time**, like the rest of the feed.
- **An empty or missing field means "not provided", not "delete this value"**, so the previous value is kept.
- **Striking a company off is final.** A later update doesn't make it active again.
- **This service is the only reader of the queue.** If another team read from the same queue, they would take
  messages away from it rather than get their own copies.
- **"Similar" means a similar type of business.** Location and company size aren't considered in this first version.
- **The API runs on an internal network** behind the portal, so it has no login.

## Scaling to 10x

Ten times the volume (about 20,000 companies) is still small for Postgres, so the design holds. What I would change:

| Change | Why |
|---|---|
| **Embed in batches** | Embedding is the slowest step. Processing many at once is much faster, and it could later move to a separate worker |
| **Run several consumers** | "Newest wins" doesn't depend on arrival order, so several consumers can share the work, once the ordering issue below is fixed |
| **Cache results** | Recommendations only change when a company changes, so they can be cached and refreshed when that happens |
| **Tune the search index** | Only needed once the data reaches millions of companies |

## What's left

| Item | Problem | Fix |
|---|---|---|
| **Ordering edge case** (highest priority) | If a strike-off (or one of 5 empty updates) arrives before a company's details, the details can be wrongly skipped. Never happens in the feed's own order, but can when shuffled | Don't let strike-offs or empty updates move the company's "last updated" time, and add a test that processes the feed in random order |
| **Repeated text in descriptions** | 7 descriptions repeat one phrase 60 times, which measurably weakens their matches | Collapse repeats before embedding (a small change) |
| **Companies with no description** | 39 companies (2%) get no recommendations and never appear in anyone else's | Fall back to companies in the same industry and state |
| **Better visibility** | Failed messages are logged but not recorded in the database | Record them, and monitor queue backlog, processing delay, rejections, API speed and empty panels |
| **Small API polish** | Tied results can change order; errors come in two formats | A tie-breaker, and one error format |

## Deep dive: choosing the embedding model

**The problem.** The brief suggests an English-only model. While going through the feed, I noticed the same
businesses written in two languages, such as "Boutique hotel in Penang" and "Hotel butik di Penang". 43% of the
unique descriptions are in Malay, covering about a third of companies. An English-only model would treat these as
unrelated, so those companies could quietly get poor recommendations.

**The test.** Most companies share a small set of descriptions (2,000 companies, 173 unique descriptions), so
testing on companies would mostly compare identical text. Instead, I labelled each unique description with its type
of business and language, and paired 72 Malay descriptions with their English translations. I then checked each
model with two simple questions:

- **Are its top matches sensible?** Of the 4 closest matches for each description, how many are in the same industry?
- **Does it understand Malay?** Given a Malay description such as "Hotel butik di Penang", is the closest English
  description the same type of business ("Boutique hotel in ...")?

**The results.**

| Model | Industry P@4 | Cross-lingual R@1 (business type) | Cross-lingual R@1 (exact pair) | ms / description |
|---|---|---|---|---|
| BAAI/bge-small-en-v1.5 (suggested) | 0.718 | 0.453 | 0.458 | 4.4 |
| snowflake/snowflake-arctic-embed-s | 0.705 | 0.320 | 0.319 | 3.6 |
| sentence-transformers/all-MiniLM-L6-v2 | 0.749 | 0.347 | 0.306 | 2.0 |
| **sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2** | **0.867** | **0.973** | **0.889** | 3.0 |

- **Industry P@4** (precision at 4): of each description's 4 nearest neighbours, the share in the same *industry*,
  the feed's broad category (11 values, e.g. Hospitality). Only the description is embedded, so the label can't leak in.
- **Cross-lingual R@1, business type** (recall at 1): for each of the 75 Malay descriptions, whether the single
  nearest of all 98 English descriptions is the same *business type*, a finer label I added (44 values, e.g.
  boutique hotel vs beach homestay). A different city still counts.
- **Cross-lingual R@1, exact pair:** the same, but the nearest must be its exact translation, city included (72 pairs).
- **ms / description:** embedding time on my laptop CPU.

**The decision.** The multilingual model found the right English equivalent for a Malay description 97% of the time, against
45% for the suggested model, and it was also the best on industry neighbours. It's just as small and fast, and fit
the existing setup without changes, so I chose it.

**What it doesn't prove.** Industry is only a rough stand-in for "similar", and the test set is small. It's enough to
show a 45% versus 97% difference, but not to rank two close models.

## Testing

There are 61 automated tests, all passing. They cover every cleaning rule, a run over the whole real feed, how the
consumer handles duplicates, late updates, partial updates and failures, and every API response. Not yet covered:
processing messages in a shuffled order, and speed under load.

## AI tools used

I used Claude throughout. **I** explored the data and found the issues (Claude fact-checked my list), and made the
decisions in this document: rejecting placeholder records, matching on description only, and switching to a
multilingual model based on my own measurements.

Claude drafted the first versions of the parser, database schema and update logic, which I adapted and tested. It
also helped write the API and the error handling, and wrote most of the tests, the evaluation script and the
evaluation labels, all of which I reviewed and ran. This document was drafted with Claude from my findings and
edited by me. I've read all the code and can explain any part of it.

## Time

About 6 hours. Most of it went into the consumer and data cleaning; about an hour went into the model comparison.
