# Check reference

Generated from the module registry (`scripts/gen_checks_doc.py`). Statuses: PASS, WARNING, FAIL, NOT_CHECKED (with reason), N/A (not applicable to this site).

## 1. Keyword Research & Mapping (On-page)

| Check ID | Check | Impact | Effort | Fix |
|---|---|---|---|---|
| `kw.mapping` | Keyword map (primary keyword per page) | Med | Med | Give unmapped pages a clear focus (or merge/noindex them); use the map to steer titles and H1s. |
| `kw.cannibalization` | Keyword cannibalization | Med | Med | Pick one page per query: merge or differentiate the others and link them to the chosen page. |
| `kw.owner_keywords` | Owner's target keywords without a matching page | Med | Med | Create or retarget a page for each unmatched keyword. |
| `kw.competitor_gaps` | Keyword gaps vs competitors | Med | High | Plan content for the most relevant gaps (new pages or sections). |
| `kw.search_volume` | Search volume for mapped keywords | Med | Low | Prioritise by volume and intent. |
| `kw.difficulty` | Keyword difficulty | Low | Low | Target winnable keywords first. |
| `kw.opportunities` | Keyword opportunities not covered by any page | Med | Med | Add the most relevant ones to existing pages (sections/FAQs) or create new pages. |

## 2. Content Optimization (On-page)

| Check ID | Check | Impact | Effort | Fix |
|---|---|---|---|---|
| `content.thin` | Thin content | Med | Med | Expand with useful, specific content, or merge/noindex pages that cannot be improved. |
| `content.duplicates` | Duplicate or near-duplicate pages | Med | Med | Merge duplicates, or set a canonical to the preferred page and differentiate the others. |
| `content.repeated_blocks` | Large text blocks repeated across pages | High | Med | Remove placeholder/template text and replace it with content specific to each page. |
| `content.h1` | Missing or multiple H1 headings | Med | Low | Give each page exactly one descriptive H1 that includes its main topic. |
| `content.heading_order` | Skipped heading levels | Low | Low | Use headings in order (H1 → H2 → H3) and don't pick levels for styling. |
| `content.readability` | Hard-to-read content (Flesch Reading Ease < 30) | Low | Med | Shorten sentences, use plain words, add lists and subheadings. |
| `content.gaps` | Content gaps vs search intent | High | Med | Add the suggested sections and FAQs (see the generated content brief). |

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

| Check ID | Check | Impact | Effort | Fix |
|---|---|---|---|---|
| `links.orphans` | Orphan pages (in sitemap, no internal links) | Med | Low | Link to each orphan from relevant pages (see the link suggestions), or remove it from the sitemap if it should not exist. |
| `links.few_inlinks` | Pages with fewer than 3 internal links pointing to them | Med | Low | Add contextual links from related pages and hub/category pages. |
| `links.deep_pages` | Pages more than 3 clicks from the homepage | Med | Med | Link to them from the homepage, navigation, or hub pages so they are within 3 clicks. |
| `links.generic_anchors` | Generic anchor text ("click here", "read more") | Low | Low | Use descriptive anchors that name the target topic. |
| `links.empty_anchors` | Links with no anchor text | Low | Low | Add visible text or an aria-label describing the target. |
| `links.nofollow_internal` | Internal links marked nofollow | Low | Low | Remove rel="nofollow" from internal links (use noindex on the target if needed). |
| `links.suggestions` | Internal link opportunities | Med | Low | Add the suggested links (source page → anchor → target page). |

## 5. Image SEO (On-page)

| Check ID | Check | Impact | Effort | Fix |
|---|---|---|---|---|
| `img.alt_missing` | Images missing alt attribute | Med | Low | Add descriptive alt text (what the image shows, in context). |
| `img.alt_empty` | Image links with empty alt (link has no text) | Med | Low | Give the image an alt that describes the link target (e.g. "Genesis TS22 kart"). |
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
| `tech.soft_404` | Soft 404s (missing pages return 200) | Med | Med | Return HTTP 404 (or 410) for unknown URLs; for single-page apps configure the host's fallback to send a 404 status for unknown routes. |
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

| Check ID | Check | Impact | Effort | Fix |
|---|---|---|---|---|
| `perf.lcp` | Largest Contentful Paint (LCP ≤ 2.5 s) | High | Med | Optimise the LCP element: compress/resize the hero image, preload it, cut render-blocking CSS/JS and reduce server response time. |
| `perf.inp` | Interaction to Next Paint (INP ≤ 200 ms) | High | Med | Break up long JavaScript tasks, defer third-party scripts, and reduce main-thread work. |
| `perf.cls` | Cumulative Layout Shift (CLS ≤ 0.1) | High | Low | Set width/height on images and embeds, reserve space for ads/banners, avoid inserting content above existing content. |
| `perf.tbt` | Total Blocking Time (lab proxy for INP, ≤ 200 ms) | Med | Med | Reduce and split JavaScript; defer non-critical scripts. |
| `perf.score` | Lighthouse performance score (≥ 90) | Med | Med | Work through the opportunities listed in the performance findings. |
| `perf.opportunities` | Top performance opportunities | Med | Med | Apply the listed fixes, starting with the largest savings. |

## 9. Schema Markup (Schema)

| Check ID | Check | Impact | Effort | Fix |
|---|---|---|---|---|
| `schema.syntax` | Structured data syntax and vocabulary errors | High | Low | Fix the JSON syntax, use "@context": "https://schema.org" and give every item an @type. |
| `schema.required` | Missing required schema properties | Med | Low | Add the listed required properties (see Google's structured data docs for each type). |
| `schema.recommended` | Missing recommended schema properties | Low | Low | Add the listed recommended properties where the information exists on the page. |
| `schema.conflicts` | Duplicate or conflicting schema | Med | Low | Keep one item per entity and make names/URLs consistent across the site. |
| `schema.missing_types` | Schema missing for the page type | Med | Low | Add the generated JSON-LD to the page (fill any fields listed under needs_input). |

## 10. Backlink Analysis (Off-page)

| Check ID | Check | Impact | Effort | Fix |
|---|---|---|---|---|
| `bl.authority` | Domain Authority Score | Med | High | Earn links from relevant, authoritative sites (see Link Building & Outreach). |
| `bl.discovered` | Discovered backlinks (sample) | Med | High | Build more links from relevant sites; reclaim unlinked mentions. |
| `bl.referring_domains` | Referring domains (full list) and new/lost domains | Med | High | Investigate lost referring domains; keep earning new ones. |
| `bl.suspicious` | Suspicious backlink patterns | Med | Med | Review the flagged referrers; remove paid/spam links where you can. |
| `bl.link_gap` | Competitor link gap | Med | High | Pitch these domains (they are added to the outreach list). |

## 12. Brand Mentions & Reputation (Off-page)

| Check ID | Check | Impact | Effort | Fix |
|---|---|---|---|---|
| `mentions.unlinked` | Unlinked brand mentions | Med | Low | Contact each site and ask for the mention to link to your site (drafts in Outreach). |
| `mentions.share` | Brand visibility vs competitors | Med | High | Grow mentions through PR, partnerships, reviews and community participation. |
| `mentions.reviews` | Public review sentiment | Med | Med | Address recurring complaints and ask happy customers for reviews. |

## 11. Link Building & Outreach (Off-page)

| Check ID | Check | Impact | Effort | Fix |
|---|---|---|---|---|
| `out.prospects` | Outreach prospects | Med | High | Work through the list in the Outreach tracker (highest score first). |
| `out.drafts` | Personalised outreach email drafts | Low | Med | Review, personalise further and send from your own mailbox; update the tracker status. |

## 13. Google Business Profile (Local)

_Not built yet; planned for build phase 5. Reported as NOT_CHECKED._

## 14. Citations & NAP (Local)

_Not built yet; planned for build phase 5. Reported as NOT_CHECKED._

## 15. Review Management (Local)

_Not built yet; planned for build phase 5. Reported as NOT_CHECKED._

## 16. AI Search Visibility (AI Search & GEO)

| Check ID | Check | Impact | Effort | Fix |
|---|---|---|---|---|
| `ai.crawler_access` | AI crawlers allowed in robots.txt | Med | Low | Allow the AI crawlers you want to be visible in (or keep blocks you chose deliberately). |
| `ai.llms_txt` | llms.txt file | Low | Low | Publish /llms.txt (a draft is generated below). |
| `ai.visibility` | Brand visibility in AI answers (sample) | High | High | Strengthen GEO content, earn mentions on sites AI assistants cite, and keep AI crawlers allowed. |

## 17. GEO Content Optimization (AI Search & GEO)

| Check ID | Check | Impact | Effort | Fix |
|---|---|---|---|---|
| `geo.answer_first` | Direct answer near the top of the page | High | Low | Open with a 40-60 word answer/definition (see the generated rewrite), then go into detail. |
| `geo.question_headings` | Question-style headings | Low | Low | Rephrase some H2/H3 headings as the questions they answer. |
| `geo.faq` | FAQ section on key pages | Med | Low | Add a short FAQ section (AI drafts are attached where available; verify the answers). |
| `geo.sources_and_stats` | Statistics and cited sources | Med | Med | Add specific figures and link to authoritative sources for key claims. |
| `geo.authorship` | Author bylines and dates on articles | Med | Low | Show author name (linked to a bio) and published/updated dates on every article. |
| `geo.eeat_site` | Site trust signals (about, contact, policies, profiles) | Med | Low | Add the missing pages/signals listed in the evidence. |
