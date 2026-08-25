/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_RECEIVER_LAB_BASE_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
