/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** hosted | legacy | static. Default (unset) = legacy (Vercel Python live functions). */
  readonly VITE_ANALYSIS_BACKEND?: string;
  /** Same-origin proxy prefix for the hosted API. Default /hosted-api. */
  readonly VITE_HOSTED_API_BASE?: string;
}
