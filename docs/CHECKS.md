# Check reference

Generated from the module registry (`scripts/gen_checks_doc.py`). Statuses: PASS, WARNING, FAIL, NOT_CHECKED (with reason), N/A (not applicable to this site).

## 1. Keyword Research & Mapping (On-page)

_Not built yet; planned for build phase 3. Reported as NOT_CHECKED._

## 2. Content Optimization (On-page)

_Not built yet; planned for build phase 3. Reported as NOT_CHECKED._

## 3. Meta Tags (On-page)

| Check ID | Check | Impact | Effort | Fix |
|---|---|---|---|---|
| `meta.title_missing` | Missing title tag | High | Low | Write a unique, descriptive title (~50–60 characters) for each page. |
| `meta.title_multiple` | Multiple title tags | Low | Low | Keep exactly one <title> in <head>. |
| `meta.title_duplicate` | Duplicate titles | Med | Med | Give each page a unique title that reflects its specific content. |
| `meta.title_too_long` | Title too long (truncated in results) | Low | Low | Shorten to ~50–60 characters, putting the main keyword first. |
| `meta.title_too_short` | Title too short | Low | Low | Expand to ~50–60 characters with the main topic and a differentiator. |
| `meta.description_missing` | Missing meta description | Med | Low | Write a unique 120–160 character description that summarises the page and invites the click. |
| `meta.description_multiple` | Multiple meta descriptions | Low | Low | Keep exactly one meta description. |
| `meta.description_duplicate` | Duplicate meta descriptions | Med | Med | Write a unique description for each page. |
| `meta.description_length` | Meta description too short or too long | Low | Low | Aim for 120–160 characters. |
| `meta.social_tags` | Missing Open Graph / Twitter tags | Low | Low | Add og:title, og:description, og:image (and twitter:card). |
| `meta.keyword_in_title` | Primary keyword present in title | Med | Low | Include the primary keyword near the start of the title. |
| `meta.intent_alignment` | Title/description match search intent | Med | Med | Rewrite to match what the searcher wants (learn, compare, buy, find). |

## 4. Internal Linking (On-page)

_Not built yet; planned for build phase 2. Reported as NOT_CHECKED._

## 5. Image SEO (On-page)

| Check ID | Check | Impact | Effort | Fix |
|---|---|---|---|---|
| `img.alt_missing` | Images missing alt attribute | Med | Low | Add descriptive alt text (what the image shows, in context). |
| `img.alt_empty` | Empty alt on meaningful images | Low | Low | Describe content images and image links; keep alt="" only for decorative images. |
| `img.filename` | Non-descriptive image file names | Low | Med | Rename files to short descriptive slugs (e.g. red-leather-sofa.jpg) and update references. |
| `img.file_size` | Heavy image files | Med | Med | Compress and resize; serve WebP/AVIF at the displayed size. |
| `img.oversized_dimensions` | Images much larger than their display size | Med | Med | Resize to ~2× the display width and use srcset/sizes for responsive images. |
| `img.legacy_format` | Images not in a modern format (WebP/AVIF) | Low | Med | Convert to WebP or AVIF (or serve via <picture> with a JPEG/PNG fallback). |
| `img.missing_dimensions` | Images without width/height attributes | Med | Low | Add width and height attributes matching the image's aspect ratio. |
| `img.lazy_missing` | Below-the-fold images not lazy-loaded | Low | Low | Add loading="lazy" to images below the fold. |
| `img.lazy_above_fold` | Lazy-loading on the first/hero image | Med | Low | Remove loading="lazy" from the first visible image (consider fetchpriority="high"). |
| `img.broken` | Broken images | Med | Low | Fix the image URL or remove the reference. |
| `img.alt_generation` | AI-generated alt text suggestions | Med | Low | Review and paste the generated alt text. |

## 6. Technical Audit (Technical)

| Check ID | Check | Impact | Effort | Fix |
|---|---|---|---|---|
| `tech.status_4xx` | Pages returning 4xx errors | High | Low | Restore the page, 301-redirect it to the closest live page, or remove the links pointing to it. |
| `tech.status_5xx` | Pages returning 5xx server errors | High | Med | Check server logs for these URLs and fix the application or hosting error. |
| `tech.fetch_errors` | Pages that timed out or failed to load | Med | Med | Check server performance and firewall/bot-protection rules for these URLs. |
| `tech.broken_internal_links` | Broken internal links | High | Low | Update each link to point at a live URL (or remove it). |
| `tech.broken_external_links` | Broken external links | Low | Low | Replace with a working source or remove the link. |
| `tech.redirect_chains` | Redirect chains (more than one hop) | Med | Low | Point the first URL straight at the final destination with a single 301. |
| `tech.redirect_loops` | Redirect loops | High | Low | Fix the redirect rules so the URL resolves to a 200 page. |
| `tech.links_to_redirects` | Internal links pointing to redirects | Low | Low | Update internal links to the final destination URL. |
| `tech.canonical_missing` | Missing canonical tag | Low | Low | Add a self-referencing <link rel="canonical"> to every indexable page. |
| `tech.canonical_broken` | Canonical points to a non-200 URL | High | Low | Point the canonical at the live, final (200) version of the page. |
| `tech.canonical_conflict` | Conflicting canonical signals | Med | Low | Keep exactly one canonical in <head>, consistent between HTML, HTTP header and rendered DOM. |
| `tech.noindex_important` | Noindex on important pages | High | Low | Remove the noindex directive (meta robots or X-Robots-Tag) if the page should rank. |
| `tech.hreflang` | Hreflang errors | Med | Med | Use valid ISO codes, include a self-reference, make every alternate link back, and point only at 200 canonical URLs. |
| `tech.https` | HTTPS and HTTP→HTTPS redirect | High | Med | Serve the site over HTTPS and 301-redirect every HTTP URL to its HTTPS version. |
| `tech.mixed_content` | Mixed content on HTTPS pages | Med | Low | Load every resource over HTTPS (update URLs or use protocol-relative/HTTPS sources). |
| `tech.url_variants` | Duplicate URL variants (www / trailing slash) | Med | Low | 301-redirect all variants to one preferred host and URL format. |
| `tech.viewport` | Missing or incorrect viewport meta tag | High | Low | Add <meta name="viewport" content="width=device-width, initial-scale=1">. |
| `tech.lang` | Missing html lang attribute | Low | Low | Add lang to the <html> element, e.g. <html lang="en">. |
| `tech.slow_response` | Slow server response | Med | Med | Add caching (page/CDN), optimize backend queries, or upgrade hosting. |
| `tech.js_dependent` | Content that only appears after JavaScript | Med | High | Server-side render or pre-render primary content and navigation links. |
| `tech.render_mismatch` | Raw vs rendered HTML disagree on key tags | Med | Med | Output the final title, canonical and robots directives in the server HTML. |
| `tech.meta_refresh` | Meta refresh redirects | Low | Low | Replace meta refresh with a server-side 301 redirect. |

## 7. Crawling & Indexing (Technical)

| Check ID | Check | Impact | Effort | Fix |
|---|---|---|---|---|
| `crawl.robots_txt` | robots.txt available | Med | Low | Serve a valid robots.txt at the site root that returns 200 (or 404 if you want no rules). |
| `crawl.robots_blocks_important` | robots.txt blocks important URLs | High | Low | Remove or narrow the Disallow rules that match these URLs. |
| `crawl.robots_blocks_resources` | robots.txt blocks CSS/JS needed for rendering | Med | Low | Allow crawling of CSS and JavaScript files. |
| `crawl.robots_sitemap_directive` | robots.txt references the sitemap | Low | Low | Add 'Sitemap: https://example.com/sitemap.xml' to robots.txt. |
| `crawl.sitemap_present` | XML sitemap present | Med | Low | Publish an XML sitemap of indexable URLs and reference it in robots.txt. |
| `crawl.sitemap_errors` | Sitemap files load and parse | Med | Low | Fix the sitemap URL/HTTP status and make sure it is valid sitemap XML. |
| `crawl.sitemap_bad_urls` | Sitemap lists non-200 URLs | Med | Low | List only final, 200-status URLs. |
| `crawl.sitemap_non_indexable` | Sitemap lists noindexed, canonicalised or blocked URLs | Med | Low | Remove these URLs from the sitemap, or make them indexable. |
| `crawl.sitemap_missing_pages` | Indexable pages missing from the sitemap | Med | Low | Add every indexable, canonical page to the sitemap. |
| `crawl.noindex_linked` | Noindexed pages that receive many internal links | Low | Med | Either make these pages indexable or reduce internal links to them. |
| `crawl.gsc_index_coverage` | Indexed vs not-indexed pages (Search Console) | High | Low | Review 'Crawled – currently not indexed' and 'Discovered – currently not indexed' URLs. |
| `crawl.gsc_url_inspection` | URL Inspection for key pages (Search Console) | Med | Low | Fix issues reported for key pages. |

## 8. Website Performance (Performance)

_Not built yet; planned for build phase 2. Reported as NOT_CHECKED._

## 9. Schema Markup (Schema)

_Not built yet; planned for build phase 2. Reported as NOT_CHECKED._

## 10. Backlink Analysis (Off-page)

_Not built yet; planned for build phase 4. Reported as NOT_CHECKED._

## 11. Link Building & Outreach (Off-page)

_Not built yet; planned for build phase 4. Reported as NOT_CHECKED._

## 12. Brand Mentions & Reputation (Off-page)

_Not built yet; planned for build phase 4. Reported as NOT_CHECKED._

## 13. Google Business Profile (Local)

_Not built yet; planned for build phase 5. Reported as NOT_CHECKED._

## 14. Citations & NAP (Local)

_Not built yet; planned for build phase 5. Reported as NOT_CHECKED._

## 15. Review Management (Local)

_Not built yet; planned for build phase 5. Reported as NOT_CHECKED._

## 16. AI Search Visibility (AI Search & GEO)

_Not built yet; planned for build phase 4. Reported as NOT_CHECKED._

## 17. GEO Content Optimization (AI Search & GEO)

_Not built yet; planned for build phase 3. Reported as NOT_CHECKED._
