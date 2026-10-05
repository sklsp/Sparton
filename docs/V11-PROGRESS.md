# v1.1 progress

- M1 done: shopfeed installed in the image via a BuildKit secret (`gh_token`), build without it warns and falls back; token absent from history, inspect, image filesystem and build log (verified on real builds). Docs: README, DEPLOYMENT, LAUNCH, D-036.
- M2 done: own catalogue read with shopfeed (`shop_products`), matches via shopfeed.match stored in `product_matches` (barcode first, title rules), `vs_you` gap on /changes and /overview, own price line on /changes/{id}/history and the chart, low-confidence matches labelled. 12 new tests. D-037.
- M3 done: report prose in the account language (organizations.language, set at signup and by the NL/EN switch via PUT /auth/language). Model gets Dutch instructions plus the same FACTS; the no-model fallback is Dutch too; reports record their language. 9 new tests with the test provider. D-038.
- M4 done: copy that said matching/own price/Dutch reports were missing is updated in NL and EN (chart legend now says "no match in your own shop yet" only when there is none; the reports note appears only when a report is in the other language, and the prose carries its lang attribute). i18n test green.
