/// <reference types="vite/client" />

// The build's settings (deploy.yml passes them from the repo's Actions
// variables). Both unset: the site as it was before the API, with no calls.
interface ImportMetaEnv {
  /** the API's base URL, e.g. https://rigged-outcomes-api.<sub>.workers.dev */
  readonly VITE_API_URL?: string;
  /** the Turnstile widget's site key (public; the secret stays in the Worker) */
  readonly VITE_TURNSTILE_SITEKEY?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
