# v1.1 progress

- M1 done: shopfeed installed in the image via a BuildKit secret (`gh_token`), build without it warns and falls back; token absent from history, inspect, image filesystem and build log (verified on real builds). Docs: README, DEPLOYMENT, LAUNCH, D-036.
- M2 done: own catalogue read with shopfeed (`shop_products`), matches via shopfeed.match stored in `product_matches` (barcode first, title rules), `vs_you` gap on /changes and /overview, own price line on /changes/{id}/history and the chart, low-confidence matches labelled. 12 new tests. D-037.
