# Bundled typefaces — provenance and licence

Three font files ship in this directory and are inlined into the dashboard's stylesheet as
data URIs by `mdq.dashboard.theme`. They are vendored rather than fetched from a CDN so the
dashboard renders in its intended typography on a machine with no network — which matters,
because this project is meant to be cloned and run.

Both families are licensed under the **SIL Open Font License, Version 1.1**, whose full text
is included here as required. Neither licence restricts this use, and neither font is
modified: the files are the unaltered `latin` subsets as served by Google Fonts.

| File | Family | Weights | Licence |
|---|---|---|---|
| `notosans-400-800-latin.woff2` | Noto Sans (variable) | 400–800 | [OFL 1.1](./OFL-NotoSans.txt) |
| `courierprime-400-latin.woff2` | Courier Prime | 400 | [OFL 1.1](./OFL-CourierPrime.txt) |
| `courierprime-700-latin.woff2` | Courier Prime | 700 | [OFL 1.1](./OFL-CourierPrime.txt) |

## Copyright notices

Retained here as the OFL requires:

> Copyright 2022 The Noto Project Authors
> (https://github.com/notofonts/latin-greek-cyrillic)

> Copyright 2015 The Courier Prime Project Authors
> (https://github.com/quoteunquoteapps/CourierPrime)

## How these files were obtained

Fetched 2026-09-16 from the Google Fonts CSS API, taking only the `latin` subset of each
face — 58 KB in total, against roughly 400 KB for the full set of subsets:

```
https://fonts.googleapis.com/css2?family=Noto+Sans:wght@400..800&family=Courier+Prime:wght@400;700&display=swap
```

The API returns one `@font-face` block per subset; the `src` URL of each block labelled
`/* latin */` was downloaded verbatim from `fonts.gstatic.com`. All three files carry the
`wOF2` signature. The licence texts come from
`https://github.com/google/fonts/tree/main/ofl/{notosans,courierprime}/OFL.txt`.

To refresh them, request that CSS URL with a modern browser `User-Agent` (an older one is
served TTF instead of WOFF2), take the `latin` block's URL for each face, and replace the
files in place — the family names and weights are what `theme.py` and
`.streamlit/config.toml` reference, so keeping those stable is the only constraint.

## Why these two faces

Noto Sans is the freely licensable half of Macquarie Group's own published type system,
which pairs a proprietary face, *MCQ Global*, with Noto Sans as its global complement. MCQ
Global is not licensable for this project and is not used or imitated here. Courier Prime
is the typed-onto-the-form voice, used only for measurements, report numbers, thresholds and
timestamps — never as decoration. See `PRODUCT.md` § Brand Commitments.
