"""Configuration.

Two layers:

* ``EnvSettings``: secrets and deployment wiring, read from environment variables
  (Replit Secrets). Never stored in the database, never returned by the API.
* ``RuntimeSettings``: tunables (crawl limits, budgets, scoring weights, thresholds)
  with defaults here and overrides stored in the ``settings`` table, editable in the UI.
"""
from __future__ import annotations

import os
import ssl
from functools import lru_cache
from typing import Literal

import certifi
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app import __version__


class EnvSettings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://seo:seo@localhost:5432/seo"

    # Web app auth
    admin_password: str | None = None
    session_secret: str | None = None
    session_cookie_secure: bool = False
    import_api_token: str | None = None  # Colab import endpoint (Phase 4)

    # Integrations (presence is reported in Settings; values never leave the process)
    anthropic_api_key: str | None = None
    brave_api_key: str | None = None
    pagespeed_api_key: str | None = None
    google_ads_developer_token: str | None = None
    google_ads_client_id: str | None = None
    google_ads_client_secret: str | None = None
    google_ads_refresh_token: str | None = None
    google_ads_customer_id: str | None = None
    gsc_service_account_json: str | None = None
    gbp_service_account_json: str | None = None
    gcp_service_account_json: str | None = None  # Vertex AI (Gemini)
    gcp_project: str | None = None
    gcp_location: str = "us-central1"

    # Storage
    blob_backend: Literal["auto", "replit", "postgres"] = "auto"
    object_storage_bucket_id: str | None = None

    # Crawler identity
    crawler_contact_url: str | None = None
    playwright_chromium_path: str | None = None

    # Process
    port: int = 8000
    log_level: str = "INFO"

    @field_validator("database_url")
    @classmethod
    def _sqlalchemy_driver(cls, v: str) -> str:
        # Replit provides postgres:// or postgresql:// URLs; we use psycopg 3.
        if v.startswith("postgres://"):
            v = "postgresql://" + v[len("postgres://"):]
        if v.startswith("postgresql://"):
            v = "postgresql+psycopg://" + v[len("postgresql://"):]
        return v

    @property
    def user_agent(self) -> str:
        contact = f"; +{self.crawler_contact_url}" if self.crawler_contact_url else "; internal SEO audit"
        return f"Mozilla/5.0 (compatible; SEOInspector/{__version__}{contact})"

    def integration_status(self) -> dict[str, bool]:
        ads = all([self.google_ads_developer_token, self.google_ads_client_id,
                   self.google_ads_client_secret, self.google_ads_refresh_token,
                   self.google_ads_customer_id])
        return {
            "anthropic": bool(self.anthropic_api_key),
            "brave_search": bool(self.brave_api_key),
            "pagespeed": bool(self.pagespeed_api_key),
            "google_ads_keyword_planner": ads,
            "search_console": bool(self.gsc_service_account_json),
            "google_business_profile": bool(self.gbp_service_account_json),
            "vertex_ai_gemini": bool(self.gcp_service_account_json and self.gcp_project),
            "admin_password": bool(self.admin_password),
            "session_secret": bool(self.session_secret),
            "import_api_token": bool(self.import_api_token),
        }


@lru_cache
def env() -> EnvSettings:
    return EnvSettings()


def ssl_context() -> ssl.SSLContext:
    """Trust store for outbound HTTPS: an explicit bundle from the environment, else certifi."""
    cafile = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE") or certifi.where()
    return ssl.create_default_context(cafile=cafile)


# --------------------------------------------------------------------------- runtime


class CrawlSettings(BaseModel):
    max_pages: int = Field(100, ge=1, le=5000, description="HTML pages fetched per audit")
    rate_limit_per_sec: float = Field(2.0, gt=0, le=2.0, description="Max requests/second per site")
    concurrency: int = Field(4, ge=1, le=8)
    timeout_s: float = Field(20.0, ge=3, le=60)
    retries: int = Field(2, ge=0, le=5)
    max_html_bytes: int = Field(5_000_000, ge=100_000, le=20_000_000)
    render_mode: Literal["auto", "never", "always"] = "auto"
    max_rendered_pages: int = Field(100, ge=0, le=1000)
    render_sample_templates: int = Field(5, ge=0, le=20, description="Extra pages rendered to measure image display sizes")
    render_timeout_s: float = Field(25.0, ge=5, le=90)
    max_internal_link_checks: int = Field(200, ge=0, le=5000)
    max_external_link_checks: int = Field(150, ge=0, le=5000)
    max_image_checks: int = Field(300, ge=0, le=5000)
    max_sitemap_urls: int = Field(50_000, ge=100, le=500_000)
    sitemap_check_limit: int = Field(100, ge=0, le=5000, description="Uncrawled sitemap URLs status-checked")
    include_subdomains: bool = False
    user_agent_token: str = "SEOInspector"


class BudgetSettings(BaseModel):
    brave_queries_per_audit: int = Field(60, ge=0)
    brave_queries_per_month: int = Field(1000, ge=0)
    llm_usd_per_audit: float = Field(2.0, ge=0)
    llm_usd_per_month: float = Field(30.0, ge=0)
    gemini_usd_per_month: float = Field(10.0, ge=0)
    alt_text_images_per_audit: int = Field(30, ge=0)
    keyword_difficulty_keywords: int = Field(20, ge=0)
    pagespeed_templates: int = Field(5, ge=0)
    ai_visibility_prompts: int = Field(10, ge=0)
    ai_visibility_runs: int = Field(2, ge=1)


class ThresholdSettings(BaseModel):
    title_min_chars: int = 30
    title_max_chars: int = 60
    title_max_px: int = 580
    description_min_chars: int = 120
    description_max_chars: int = 160
    image_warn_kb: int = 150
    image_fail_kb: int = 500
    image_oversize_ratio: float = 2.0
    image_legacy_min_kb: int = 10
    slow_response_ms: int = 1000
    important_page_min_inlinks: int = 3
    thin_content_words: int = 200


class ScoringSettings(BaseModel):
    category_weights: dict[str, float] = Field(default_factory=lambda: {
        "technical": 25, "onpage": 30, "performance": 15, "schema": 5,
        "offpage": 10, "local": 5, "geo": 10,
    })


class LLMSettings(BaseModel):
    reasoning_model: str = Field("claude-sonnet-5-5", description="Keyword mapping, intent, content gaps, rewrites, GEO")
    bulk_model: str = Field("claude-haiku-5-5", description="Alt text (vision) and other bulk tasks")
    reasoning_effort: Literal["low", "medium", "high"] = "low"
    bulk_effort: Literal["low", "medium", "high"] = "low"
    max_pages: int = Field(60, ge=0, le=500, description="Pages analysed by the LLM per audit (most important first)")
    content_gap_pages: int = Field(20, ge=0, le=200)
    geo_pages: int = Field(15, ge=0, le=200)
    link_suggestions_reviewed: int = Field(60, ge=0, le=500)
    competitor_pages: int = Field(5, ge=0, le=20, description="Pages fetched per competitor URL")
    cache_days: int = Field(30, ge=0, le=365)
    refusal_fallbacks: bool = Field(True, description="Server-side fallback when the reasoning model declines")
    gemini_model: str = Field("gemini-2.5-flash", description="Vertex AI model for AI-visibility checks (verify availability)")
    gemini_usd_per_m_input: float = Field(0.30, ge=0, description="Check current Vertex AI pricing")
    gemini_usd_per_m_output: float = Field(2.50, ge=0, description="Check current Vertex AI pricing")
    gemini_usd_per_grounded_request: float = Field(0.035, ge=0, description="Google Search grounding fee (check pricing)")


class DataLayerSettings(BaseModel):
    authority_top_n: int = Field(1_000_000, ge=10_000, le=5_000_000, description="Common Crawl domains loaded")
    authority_log_reference: int = Field(100_000_000, ge=1_000_000, description="Rank mapped to score 0 (log scale)")
    keyword_cache_days: int = Field(30, ge=1, le=365)
    search_cache_days: int = Field(7, ge=1, le=90)
    autocomplete_seeds: int = Field(8, ge=0, le=50, description="Keywords expanded with Google Autocomplete")
    autocomplete_alphabet: bool = Field(True, description="Also expand seeds with a-z and question words")
    trends_enabled: bool = Field(True, description="Use Google Trends (best-effort; often rate-limited on servers)")
    difficulty_weights: dict[str, float] = Field(default_factory=lambda: {
        "authority": 0.55, "content_depth": 0.15, "exact_match_titles": 0.15, "intent_match": 0.15})
    backlink_pages_checked: int = Field(25, ge=0, le=200, description="Search results fetched to verify links")
    outreach_prospects: int = Field(20, ge=0, le=100)


class RetentionSettings(BaseModel):
    page_text_audits_per_site: int = Field(3, ge=1, le=50)
    cache_cleanup_days: int = Field(1, ge=1)


class RuntimeSettings(BaseModel):
    crawl: CrawlSettings = Field(default_factory=CrawlSettings)
    budgets: BudgetSettings = Field(default_factory=BudgetSettings)
    thresholds: ThresholdSettings = Field(default_factory=ThresholdSettings)
    scoring: ScoringSettings = Field(default_factory=ScoringSettings)
    retention: RetentionSettings = Field(default_factory=RetentionSettings)
    llm: LLMSettings = Field(default_factory=LLMSettings)
    data: DataLayerSettings = Field(default_factory=DataLayerSettings)
