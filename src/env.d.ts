/// <reference path="../.astro/types.d.ts" />

interface ImportMetaEnv {
  /** GA4 の測定ID（例: G-XXXXXXXXXX）。未設定なら計測タグは出力されない */
  readonly PUBLIC_GA_MEASUREMENT_ID?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
