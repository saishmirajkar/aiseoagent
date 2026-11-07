"""Plain-language layer for reports: each module is presented as an "agent" with a simple job description,
and each check gets a jargon-free title, a sentence on what the problem means for the business, and what to
do about it. Wording only; numbers always come from the audit itself."""
from __future__ import annotations

import re
from typing import Any

DESIGNER = "Your web designer or developer"
WRITER = "You, or whoever writes your website text"
MARKETING = "You, or whoever handles your marketing"

# module key -> agent
AGENTS: dict[str, dict[str, str]] = {
    "keywords": {"name": "Keyword Agent", "icon": "🔑", "who": WRITER,
                 "job": "Finds the words your customers type into Google and checks that each page is aimed at the right ones."},
    "content": {"name": "Content Agent", "icon": "📝", "who": WRITER,
                "job": "Reads your pages like a visitor would and checks they are useful, original and easy to read."},
    "meta_tags": {"name": "Search Listing Agent", "icon": "🔎", "who": DESIGNER,
                  "job": "Checks the headline and short description Google shows for each of your pages in search results."},
    "internal_links": {"name": "Navigation Agent", "icon": "🧭", "who": DESIGNER,
                       "job": "Checks that your pages link to each other, so visitors and Google can find everything."},
    "images": {"name": "Image Agent", "icon": "🖼️", "who": DESIGNER,
               "job": "Checks that your pictures load quickly and have descriptions Google and blind visitors can understand."},
    "technical": {"name": "Website Health Agent", "icon": "🩺", "who": DESIGNER,
                  "job": "Looks for broken pages, dead links, security warnings and other faults that put visitors and Google off."},
    "indexing": {"name": "Google Access Agent", "icon": "🚪", "who": DESIGNER,
                 "job": "Checks that Google is allowed into your site and has a complete list of your pages."},
    "performance": {"name": "Speed Agent", "icon": "⚡", "who": DESIGNER,
                    "job": "Measures how fast your site loads and responds, especially on mobile phones."},
    "schema": {"name": "Google Labels Agent", "icon": "🏷️", "who": DESIGNER,
               "job": "Checks for hidden labels that tell Google what each page is: your company, a product, an article or a video."},
    "backlinks": {"name": "Link Votes Agent", "icon": "🔗", "who": MARKETING,
                  "job": "Finds other websites that link to you. Google treats each of these links as a vote of trust."},
    "outreach": {"name": "Outreach Agent", "icon": "✉️", "who": MARKETING,
                 "job": "Finds websites that might link to you and drafts emails you can send. Nothing is ever sent automatically."},
    "brand_mentions": {"name": "Reputation Agent", "icon": "📣", "who": MARKETING,
                       "job": "Looks for where your business is talked about online and what people say about it."},
    "gbp": {"name": "Google Maps Agent", "icon": "📍", "who": MARKETING,
            "job": "Will check your Google Business Profile, the listing people see on Google Maps."},
    "citations": {"name": "Directory Listings Agent", "icon": "📒", "who": MARKETING,
                  "job": "Will check that your name, address and phone number are the same on every online directory."},
    "reviews": {"name": "Reviews Agent", "icon": "⭐", "who": MARKETING,
                "job": "Will track your customer reviews and help you reply to them."},
    "ai_visibility": {"name": "AI Assistant Agent", "icon": "🤖", "who": DESIGNER,
                      "job": "Checks whether ChatGPT, Claude and other AI assistants can read your site and recommend you."},
    "geo_content": {"name": "AI Answers Agent", "icon": "💬", "who": WRITER,
                    "job": "Checks that your pages are written so AI assistants can easily quote them in their answers."},
}

STATUS_WORDS = {"FAIL": "Needs fixing", "WARNING": "Could be better", "PASS": "Looks good",
                "NOT_CHECKED": "Couldn't check", "N/A": "Doesn't apply"}
STATUS_ICONS = {"FAIL": "🔴", "WARNING": "🟠", "PASS": "🟢", "NOT_CHECKED": "⚪", "N/A": "⚪"}

# population -> (singular, plural)
NOUNS = {"pages": ("page", "pages"), "images": ("image", "images"), "links": ("link", "links"),
         "tests": ("speed test", "speed tests"), "sitemap_urls": ("address in your page list", "addresses in your page list")}

# check id -> t: plain title, bad: the problem (a phrase after "N pages ..." for page/image/link checks,
# otherwise a full sentence), why: what it means for the business, do: what to do, who: optional override
P: dict[str, dict[str, str]] = {
    # 1 keywords
    "kw.mapping": {"t": "Each page aims at a clear search phrase",
                   "bad": "without one clear search phrase they are aimed at",
                   "why": "Google ranks a page for what it is clearly about. Pages without a clear focus rarely show up for anything.",
                   "do": "Use the keyword plan in the technical details: give each page one main phrase and use it in its headline and text."},
    "kw.cannibalization": {"t": "Pages competing for the same search",
                           "bad": "competing with another of your pages for the same search phrase",
                           "why": "When two of your pages chase the same phrase, Google splits its attention and often neither ranks well.",
                           "do": "Give each page a different focus, or merge the pages into one stronger page."},
    "kw.owner_keywords": {"t": "A page for each phrase you want to be found for",
                          "bad": "Some of the phrases you want to be found for have no page aimed at them.",
                          "why": "Google can only show you for a search if one of your pages is clearly about it.",
                          "do": "Create or adjust a page for each missing phrase."},
    "kw.competitor_gaps": {"t": "Topics your competitors cover and you don't",
                           "bad": "Your competitors have pages on topics you don't cover.",
                           "why": "Each missing topic is a group of customers your competitors can reach and you can't.",
                           "do": "Add pages for the most relevant missing topics."},
    "kw.search_volume": {"t": "How often people search for your phrases",
                         "bad": "We couldn't measure how often people search for your phrases.",
                         "why": "Knowing which phrases are searched most tells you where to focus first.",
                         "do": "Focus your work on the phrases with the most searches."},
    "kw.difficulty": {"t": "How hard it is to rank for your phrases",
                      "bad": "Some of your target phrases are very hard to rank for.",
                      "why": "Phrases dominated by big, well-known websites take much longer to win.",
                      "do": "Start with easier, more specific phrases and build up."},
    "kw.opportunities": {"t": "Searches you could be found for but aren't",
                         "bad": "People search for things you offer, but none of your pages is aimed at those searches.",
                         "why": "These are real searches by potential customers. Today they find someone else.",
                         "do": "Pick the most relevant ideas from the list and write a page or a section for each."},
    # 2 content
    "content.thin": {"t": "Pages with very little text",
                     "bad": "with very little useful text",
                     "why": "Pages with almost nothing to read rarely appear in Google and make the site look unfinished.",
                     "do": "Add useful text: what it is, who it's for, key details and common questions."},
    "content.duplicates": {"t": "Pages that are copies of each other",
                           "bad": "copied or nearly copied from another page",
                           "why": "Google shows only one of the copies and may ignore the rest.",
                           "do": "Make each page unique, or remove the copies and point them to the main page."},
    "content.repeated_blocks": {"t": "Same text repeated on many pages",
                                "bad": "where most of the text is the same as on other pages",
                                "why": "If most of a page is repeated text (often leftover template text), it offers nothing new.",
                                "do": "Remove leftover template text and write something specific for each page."},
    "content.h1": {"t": "One main heading per page",
                   "bad": "without exactly one main heading",
                   "why": "The main heading tells visitors and Google what the page is about at a glance.",
                   "do": "Give every page one clear main heading (the 'H1') that says what the page is about."},
    "content.heading_order": {"t": "Headings in a sensible order",
                              "bad": "with headings that skip levels",
                              "why": "A tidy heading structure helps Google and screen readers understand the page.",
                              "do": "Use headings in order: main heading, then sub-headings, then smaller ones."},
    "content.readability": {"t": "Text that is easy to read",
                            "bad": "with text that is hard to read",
                            "why": "Long, dense sentences lose readers and are harder for AI assistants to quote.",
                            "do": "Use shorter sentences, simpler words and more paragraphs."},
    "content.gaps": {"t": "Pages that answer what customers want to know",
                     "bad": "missing information customers look for",
                     "why": "Google prefers the page that answers the customer's questions most completely.",
                     "do": "Add the missing topics and questions listed in the technical details (drafts are included)."},
    # 3 meta tags
    "meta.title_missing": {"t": "Every page has a Google headline",
                           "bad": "with no Google headline (title)",
                           "why": "The headline is the blue clickable text in Google results. Without it Google makes one up.",
                           "do": "Give every page its own headline. Suggested wording is in the technical details."},
    "meta.title_multiple": {"t": "One Google headline per page",
                            "bad": "with more than one Google headline",
                            "why": "With several headlines, Google may pick the wrong one.",
                            "do": "Keep a single headline per page."},
    "meta.title_duplicate": {"t": "Each page has its own Google headline",
                             "bad": "sharing the same Google headline with other pages",
                             "why": "If many pages have the same headline, Google and searchers can't tell them apart.",
                             "do": "Give each page its own headline that names what's on it. Suggestions are in the technical details."},
    "meta.title_too_long": {"t": "Google headlines that fit",
                            "bad": "with a Google headline too long to show in full",
                            "why": "Long headlines are cut off with '…' in search results.",
                            "do": "Keep headlines under about 60 characters, most important words first."},
    "meta.title_too_short": {"t": "Google headlines with enough detail",
                             "bad": "with a very short Google headline",
                             "why": "A one- or two-word headline wastes space that could tell searchers why to click.",
                             "do": "Use 30–60 characters: what the page offers, plus your brand name."},
    "meta.description_missing": {"t": "Every page has a Google summary",
                                 "bad": "with no summary for Google (meta description)",
                                 "why": "The summary is the grey text under your headline in Google. Without it Google picks random text from the page.",
                                 "do": "Write a 1–2 sentence summary per page. Suggestions are in the technical details."},
    "meta.description_multiple": {"t": "One Google summary per page",
                                  "bad": "with more than one Google summary",
                                  "why": "Several summaries send mixed signals.",
                                  "do": "Keep a single summary per page."},
    "meta.description_duplicate": {"t": "Each page has its own Google summary",
                                   "bad": "sharing the same Google summary with other pages",
                                   "why": "Identical summaries make your results look the same, so fewer people click.",
                                   "do": "Write a unique summary for each page. Suggestions are in the technical details."},
    "meta.description_length": {"t": "Google summaries of the right length",
                                "bad": "with a Google summary that is too short or too long",
                                "why": "Summaries that are too long are cut off; very short ones look thin.",
                                "do": "Aim for 120–155 characters."},
    "meta.social_tags": {"t": "Nice previews when shared on WhatsApp or social media",
                         "bad": "without a proper preview when shared",
                         "why": "When someone shares your link, a missing preview shows a plain link instead of a picture and headline.",
                         "do": "Ask your designer to add social sharing tags (title, description and image)."},
    "meta.keyword_in_title": {"t": "Google headlines include the right search phrase",
                              "bad": "Some page headlines don't include the phrase the page should be found for.",
                              "why": "Google and searchers look at the headline first to decide if a page matches the search.",
                              "do": "Put the page's main search phrase near the start of its headline."},
    "meta.intent_alignment": {"t": "Headlines that match what searchers want",
                              "bad": "Some headlines and summaries don't tell searchers what the page offers.",
                              "why": "If the listing doesn't match what people are looking for, they click on a competitor instead.",
                              "do": "Use the rewritten headlines and summaries in the technical details."},
    # 4 internal links
    "links.orphans": {"t": "Pages nobody can click through to",
                      "bad": "that no other page links to",
                      "why": "Like rooms without a door: visitors can't reach these pages and Google treats them as unimportant.",
                      "do": "Link to these pages from related pages, menus or listing pages (for example, make blog cards real links)."},
    "links.few_inlinks": {"t": "Pages with very few links pointing to them",
                          "bad": "with only one or two links from your other pages",
                          "why": "Pages that are rarely linked look less important to Google.",
                          "do": "Add links to them from related pages."},
    "links.deep_pages": {"t": "Pages buried too deep",
                         "bad": "more than 3 clicks away from the homepage",
                         "why": "Pages that take many clicks to reach are visited less by people and by Google.",
                         "do": "Link to important pages from the homepage, menus or category pages."},
    "links.generic_anchors": {"t": "Link text that says where it goes",
                              "bad": "with vague text like 'click here' or 'read more'",
                              "why": "Link text tells Google what the linked page is about. 'Click here' tells it nothing.",
                              "do": "Use descriptive link text, e.g. 'see our rental go-karts'."},
    "links.empty_anchors": {"t": "Links with no text",
                            "bad": "with no text at all",
                            "why": "Google and blind visitors can't tell where these links go.",
                            "do": "Add text or a label to each link."},
    "links.nofollow_internal": {"t": "Links between your own pages that block Google",
                                "bad": "between your own pages marked 'don't follow'",
                                "why": "This tells Google to ignore links to your own pages, so they get less credit.",
                                "do": "Remove the 'nofollow' setting from links between your own pages."},
    "links.suggestions": {"t": "Helpful links you could add between your pages",
                          "bad": "Some pages would benefit from links to related pages on your site.",
                          "why": "Linking related pages helps visitors find more and tells Google how your pages fit together.",
                          "do": "Add the suggested links (listed with suggested link text in the technical details)."},
    # 5 images
    "img.alt_missing": {"t": "Images have a description",
                        "bad": "without a text description (alt text)",
                        "why": "Google can't see pictures; it reads the description. Blind visitors' screen readers read it aloud too.",
                        "do": "Add a short description to each image. Suggestions are in the technical details."},
    "img.alt_empty": {"t": "Clickable images have a description",
                      "bad": "used as a link but with no description",
                      "why": "When a picture is the link, its description is the only clue to where the link goes.",
                      "do": "Describe where each clickable image leads."},
    "img.filename": {"t": "Image file names that make sense",
                     "bad": "with meaningless file names like IMG_1234.jpg",
                     "why": "File names are a small clue for Google Images.",
                     "do": "Rename images with descriptive words, e.g. rental-go-kart.jpg."},
    "img.file_size": {"t": "Images that aren't too heavy",
                      "bad": "with a very large file size",
                      "why": "Big images make pages slow, especially on mobile data.",
                      "do": "Compress images (free tools like Squoosh or TinyPNG) before uploading."},
    "img.oversized_dimensions": {"t": "Images not bigger than needed",
                                 "bad": "much bigger than the space they are shown in",
                                 "why": "Visitors download a large picture just to see a small one, which slows the page.",
                                 "do": "Resize images to roughly the size they are displayed at."},
    "img.legacy_format": {"t": "Images in modern, lighter formats",
                          "bad": "in older, heavier formats",
                          "why": "Modern formats (WebP, AVIF) are often half the size with the same quality.",
                          "do": "Ask your designer to serve images as WebP or AVIF."},
    "img.missing_dimensions": {"t": "Images with a reserved space",
                               "bad": "without a set size",
                               "why": "Without a set size the page jumps around while images load, which annoys visitors.",
                               "do": "Ask your designer to add width and height to images."},
    "img.lazy_missing": {"t": "Images lower down load only when needed",
                         "bad": "lower down the page loading straight away",
                         "why": "Loading every picture at once delays what visitors see first.",
                         "do": "Ask your designer to turn on 'lazy loading' for images further down the page."},
    "img.lazy_above_fold": {"t": "The main image loads immediately",
                            "bad": "where the main image at the top is set to load late",
                            "why": "The first big picture visitors see appears later than it should.",
                            "do": "Ask your designer to remove lazy loading from the top image."},
    "img.broken": {"t": "Images that don't load",
                   "bad": "failing to load",
                   "why": "Broken pictures look unprofessional.",
                   "do": "Replace or remove the broken images."},
    "img.alt_generation": {"t": "Image descriptions written for you",
                           "bad": "We wrote descriptions for images that had none. They are ready to paste in.",
                           "why": "This saves you writing them yourself.",
                           "do": "Copy the suggested descriptions from the technical details into your website."},
    # 6 technical
    "tech.status_4xx": {"t": "Pages that show 'page not found'",
                        "bad": "showing an error such as 'page not found'",
                        "why": "Visitors who land on these leave, and Google removes them from its results.",
                        "do": "Fix the pages, or redirect each old address to the best current page."},
    "tech.status_5xx": {"t": "Pages with server errors",
                        "bad": "with a server error",
                        "why": "Server errors stop visitors and Google; repeated errors get pages dropped from Google.",
                        "do": "Ask your developer or hosting company to fix the server errors."},
    "tech.fetch_errors": {"t": "Pages that didn't load",
                          "bad": "that timed out or failed to load",
                          "why": "If a page doesn't load reliably, Google can't show it.",
                          "do": "Ask your developer to check why these pages are slow or failing."},
    "tech.broken_internal_links": {"t": "Broken links on your site",
                                   "bad": "with links to pages on your site that don't work",
                                   "why": "Dead ends frustrate visitors and waste Google's time.",
                                   "do": "Update or remove the broken links (the list is in the technical details)."},
    "tech.broken_external_links": {"t": "Links to other websites that no longer work",
                                   "bad": "pointing to other websites that no longer work",
                                   "why": "Dead links make a site look neglected.",
                                   "do": "Update or remove these links."},
    "tech.redirect_chains": {"t": "Addresses that forward several times",
                             "bad": "forwarding visitors through several steps",
                             "why": "Each extra step slows visitors down; Google may give up after a few.",
                             "do": "Make old addresses forward straight to the final page."},
    "tech.redirect_loops": {"t": "Addresses that forward in a circle",
                            "bad": "stuck forwarding in a loop",
                            "why": "These pages can never be opened by anyone.",
                            "do": "Ask your developer to fix the forwarding rules."},
    "tech.links_to_redirects": {"t": "Links that go through a forwarding step",
                                "bad": "linking to old addresses that forward elsewhere",
                                "why": "It slows visitors down slightly and weakens the link.",
                                "do": "Point the links straight to the final address."},
    "tech.canonical_missing": {"t": "Pages say which address is the main one",
                               "bad": "not telling Google which address is the main one",
                               "why": "When a page can be opened at several addresses, Google has to guess which one to show.",
                               "do": "Ask your designer to add a 'canonical' tag to each page."},
    "tech.canonical_broken": {"t": "Main-address tags that point to a broken page",
                              "bad": "pointing Google to a 'main address' that doesn't work",
                              "why": "Google ignores the instruction and may show the wrong page.",
                              "do": "Point the tag at the correct, working address."},
    "tech.canonical_conflict": {"t": "Conflicting main-address tags",
                                "bad": "with conflicting 'main address' instructions",
                                "why": "Mixed instructions make Google ignore them all.",
                                "do": "Keep one clear instruction per page."},
    "tech.noindex_important": {"t": "Important pages hidden from Google",
                               "bad": "set to stay out of Google even though they look important",
                               "why": "These pages can never appear in Google results.",
                               "do": "Remove the 'noindex' setting from these pages."},
    "tech.hreflang": {"t": "Language versions set up correctly",
                      "bad": "with mistakes in their language settings",
                      "why": "Google may show the wrong language version to your visitors.",
                      "do": "Ask your developer to correct the language tags."},
    "tech.https": {"t": "The site is secure (padlock)",
                   "bad": "The site is not fully secure (no padlock, or the insecure version still opens).",
                   "why": "Browsers show 'Not secure' warnings, which scare visitors away, and Google prefers secure sites.",
                   "do": "Ask your developer to set up HTTPS and forward all visitors to it."},
    "tech.mixed_content": {"t": "Secure pages loading insecure files",
                           "bad": "loading files over an insecure connection",
                           "why": "Browsers may block those files or show a warning.",
                           "do": "Load every file over https."},
    "tech.url_variants": {"t": "Your site opens at one address only",
                          "bad": "Your site opens at more than one address (for example with and without 'www').",
                          "why": "Google may treat them as separate sites and split your credit between them.",
                          "do": "Forward all versions to one main address."},
    "tech.soft_404": {"t": "Wrong addresses show a 'page not found' message",
                      "bad": "Addresses that don't exist still open a normal page instead of 'page not found'.",
                      "why": "Google can end up listing junk addresses and gets confused about which pages are real.",
                      "do": "Ask your designer to make unknown addresses show a proper 'page not found' page."},
    "tech.viewport": {"t": "Pages fit on mobile phones",
                      "bad": "not set up to fit mobile screens",
                      "why": "Google ranks sites based on the mobile version; pages that don't fit phones lose out.",
                      "do": "Ask your designer to add the mobile 'viewport' setting."},
    "tech.lang": {"t": "Pages say which language they are in",
                  "bad": "not stating their language",
                  "why": "It helps Google and screen readers handle the page correctly.",
                  "do": "Ask your designer to set the page language."},
    "tech.slow_response": {"t": "Server answers quickly",
                           "bad": "where the server is slow to respond",
                           "why": "A slow server makes every page slow and lets Google visit fewer pages.",
                           "do": "Ask your hosting company or developer about faster hosting or caching."},
    "tech.js_dependent": {"t": "Content visible without extra loading",
                          "bad": "whose content only appears after extra scripts run",
                          "why": "Google reads such content later and less reliably.",
                          "do": "Ask your developer to make the main text and links part of the page itself."},
    "tech.render_mismatch": {"t": "Page settings that change while loading",
                             "bad": "whose important settings change while the page loads",
                             "why": "Google may see a different headline or settings than visitors do.",
                             "do": "Ask your developer to set these in the page itself."},
    "tech.meta_refresh": {"t": "Old-style page forwarding",
                          "bad": "using an old, slow way of forwarding visitors",
                          "why": "It's slower and weaker than a proper redirect.",
                          "do": "Ask your developer to use a proper (301) redirect."},
    # 7 indexing
    "crawl.robots_txt": {"t": "Google's instruction file is available",
                         "bad": "Google's instruction file (robots.txt) has a problem.",
                         "why": "If this file errors, Google may stop visiting your site.",
                         "do": "Ask your developer to fix the robots.txt file."},
    "crawl.robots_blocks_important": {"t": "Important pages blocked from Google",
                                      "bad": "blocked from Google by your instruction file",
                                      "why": "Google isn't allowed to read them, so they can't rank.",
                                      "do": "Ask your developer to unblock these pages."},
    "crawl.robots_blocks_resources": {"t": "Design files blocked from Google",
                                      "bad": "whose design or script files are blocked from Google",
                                      "why": "Google may see your pages as broken or empty.",
                                      "do": "Ask your developer to unblock these files."},
    "crawl.robots_sitemap_directive": {"t": "Instruction file points to your page list",
                                       "bad": "Google's instruction file doesn't mention where your page list is.",
                                       "why": "It's a quick way to help every search engine find all your pages.",
                                       "do": "Add a 'Sitemap:' line to robots.txt."},
    "crawl.sitemap_present": {"t": "Your site has a page list for Google (sitemap)",
                              "bad": "Your site has no page list (sitemap) for Google.",
                              "why": "A sitemap is a list of all your pages that helps Google find them quickly.",
                              "do": "Most website builders can create one automatically: switch it on and tell Google about it."},
    "crawl.sitemap_errors": {"t": "Your page list can be read",
                             "bad": "Your page list (sitemap) can't be read properly.",
                             "why": "Google ignores page lists it can't read.",
                             "do": "Ask your developer to fix the sitemap file."},
    "crawl.sitemap_bad_urls": {"t": "Page list contains only working pages",
                               "bad": "not opening properly",
                               "why": "Listing broken or forwarded addresses makes Google trust your page list less.",
                               "do": "Remove broken and forwarded addresses from the sitemap."},
    "crawl.sitemap_non_indexable": {"t": "Page list contains only pages meant for Google",
                                    "bad": "hidden from Google or pointing elsewhere",
                                    "why": "Mixed signals confuse Google.",
                                    "do": "Only list pages you want to appear in Google."},
    "crawl.sitemap_missing_pages": {"t": "All pages are in your page list",
                                    "bad": "missing from your page list (sitemap)",
                                    "why": "Google finds pages that aren't in the list more slowly.",
                                    "do": "Make sure the sitemap includes every page you want found."},
    "crawl.noindex_linked": {"t": "Many links to pages hidden from Google",
                             "bad": "hidden from Google but linked to a lot",
                             "why": "Google spends time on pages it can't show.",
                             "do": "Check whether these pages should be visible in Google, or link to them less."},
    "crawl.gsc_index_coverage": {"t": "Which pages Google has actually listed",
                                 "bad": "Some of your pages are not in Google's index.",
                                 "why": "Only pages in Google's index can appear in search results.",
                                 "do": "Check the reasons in Google Search Console and fix them."},
    "crawl.gsc_url_inspection": {"t": "Google's view of your key pages",
                                 "bad": "Google sees some of your key pages differently than expected.",
                                 "why": "This shows exactly how Google treats your most important pages.",
                                 "do": "Review the details in Google Search Console."},
    # 8 performance
    "perf.lcp": {"t": "How fast the main content appears",
                 "bad": "where the main content appears slowly (over 2.5 seconds)",
                 "why": "People leave slow pages, and Google uses speed when ranking.",
                 "do": "Compress big images and ask your designer to speed up the top of the page."},
    "perf.inp": {"t": "How quickly the page reacts to taps and clicks",
                 "bad": "where the page reacts slowly to taps and clicks",
                 "why": "A page that feels stuck frustrates visitors; Google measures this too.",
                 "do": "Ask your developer to reduce heavy scripts."},
    "perf.cls": {"t": "Page stays still while loading",
                 "bad": "where the page jumps around while loading",
                 "why": "Jumping content makes people tap the wrong thing; Google measures this.",
                 "do": "Ask your designer to reserve space for images, ads and banners."},
    "perf.tbt": {"t": "Page isn't frozen while loading",
                 "bad": "where the page is frozen for a while during loading",
                 "why": "Visitors can't use the page until it unfreezes.",
                 "do": "Ask your developer to reduce or delay heavy scripts."},
    "perf.score": {"t": "Google's overall speed score",
                   "bad": "with a low score in Google's speed test",
                   "why": "It's a summary of how fast your page loads in Google's test.",
                   "do": "Work through the speed suggestions in the technical details."},
    "perf.opportunities": {"t": "Biggest speed improvements available",
                           "bad": "Google's test lists specific changes that would make your site faster.",
                           "why": "These are the changes with the biggest time savings.",
                           "do": "Share the list in the technical details with your designer."},
    # 9 schema
    "schema.syntax": {"t": "Google labels written correctly",
                      "bad": "with Google labels that contain mistakes",
                      "why": "Google ignores labels with mistakes.",
                      "do": "Ask your developer to fix the structured data errors."},
    "schema.required": {"t": "Google labels complete",
                        "bad": "with Google labels missing essential details",
                        "why": "Incomplete labels can't produce the richer Google results (stars, prices, etc.).",
                        "do": "Fill in the missing details listed in the technical details."},
    "schema.recommended": {"t": "Google labels with all useful details",
                           "bad": "with Google labels missing useful details",
                           "why": "More details mean richer results and better understanding by AI assistants.",
                           "do": "Add the recommended details listed in the technical details."},
    "schema.conflicts": {"t": "Google labels that agree with each other",
                         "bad": "with labels that contradict each other",
                         "why": "Conflicting information confuses Google.",
                         "do": "Keep one consistent label per item."},
    "schema.missing_types": {"t": "Pages labelled for Google",
                             "bad": "without the hidden labels that tell Google what they are",
                             "why": "Labels such as 'company', 'product' or 'article' help Google show richer results (logo, contact details, products).",
                             "do": "Ask your designer to add structured data. Ready-made code is in the technical details."},
    # 10 backlinks
    "bl.authority": {"t": "How trusted your website is on the web",
                     "bad": "Your website's trust score is low or has dropped.",
                     "why": "Sites that many respected websites link to rank more easily.",
                     "do": "Earn links from relevant websites (see the Outreach Agent)."},
    "bl.discovered": {"t": "Websites we found linking to you",
                      "bad": "We found few or no websites linking to you.",
                      "why": "Each link from another website is a vote of trust in Google's eyes.",
                      "do": "Ask partners, suppliers, clients and industry sites to link to you."},
    "bl.referring_domains": {"t": "Full list of websites linking to you",
                             "bad": "Some websites stopped linking to you since the last check.",
                             "why": "Losing links can lower your rankings.",
                             "do": "Check the lost websites and ask them to restore the link where it makes sense."},
    "bl.suspicious": {"t": "Spammy websites linking to you",
                      "bad": "Some of the websites linking to you look spammy.",
                      "why": "Lots of links from low-quality sites can look like paid or fake link building.",
                      "do": "Review the flagged websites. Ask for removal where you can."},
    "bl.link_gap": {"t": "Websites that link to competitors but not to you",
                    "bad": "Some websites link to your competitors but not to you.",
                    "why": "They already link to businesses like yours, so they are the most likely to link to you too.",
                    "do": "Contact them (they are added to the outreach list)."},
    # 11 outreach
    "out.prospects": {"t": "Websites worth contacting for a link",
                      "bad": "We found websites worth contacting.",
                      "why": "Getting mentioned on relevant websites brings visitors and builds trust with Google.",
                      "do": "Work through the list in the Outreach page, best matches first."},
    "out.drafts": {"t": "Email drafts ready for you",
                   "bad": "Email drafts are ready for you to review.",
                   "why": "Saves you writing each email from scratch. Nothing is sent automatically.",
                   "do": "Review, personalise and send them from your own email."},
    # 12 brand mentions
    "mentions.unlinked": {"t": "Websites that mention you without a link",
                          "bad": "Some websites mention your business but don't link to you.",
                          "why": "These are the easiest links to win: they already know you.",
                          "do": "Politely ask them to add a link (drafts are in the Outreach page)."},
    "mentions.share": {"t": "How often you're mentioned compared with competitors",
                       "bad": "Competitors are mentioned on more websites than you.",
                       "why": "The more you're talked about online, the more Google and AI assistants trust you.",
                       "do": "Grow mentions through PR, partnerships, events and customer reviews."},
    "mentions.reviews": {"t": "What people say about you in public reviews",
                         "bad": "Public reviews about you are mixed or negative.",
                         "why": "Reviews affect who clicks on you and how AI assistants describe your business.",
                         "do": "Address recurring complaints and ask happy customers to leave a review."},
    # 13-15 planned
    "gbp.module": {"t": "Your Google Maps listing", "bad": "", "why": "", "do": ""},
    "citations.module": {"t": "Your listings in online directories", "bad": "", "why": "", "do": ""},
    "reviews.module": {"t": "Your customer reviews", "bad": "", "why": "", "do": ""},
    # 16 ai visibility
    "ai.crawler_access": {"t": "AI assistants are allowed to read your site",
                          "bad": "Your site blocks some AI assistants from reading it.",
                          "why": "If ChatGPT, Claude or Perplexity can't read your site, they can't recommend you.",
                          "do": "Ask your developer to allow AI crawlers in robots.txt."},
    "ai.llms_txt": {"t": "A guide file for AI assistants",
                    "bad": "Your site has no llms.txt guide for AI assistants.",
                    "why": "It's a new, optional file that points AI tools to your most important pages.",
                    "do": "Optional: add an llms.txt file. A draft is in the technical details."},
    "ai.visibility": {"t": "Do AI assistants recommend you?",
                      "bad": "AI assistants rarely mention you when people ask about your market.",
                      "why": "More and more customers ask AI assistants for recommendations instead of searching Google.",
                      "do": "Follow the AI Answers Agent's advice and earn mentions on trusted websites."},
    # 17 geo content
    "geo.answer_first": {"t": "Pages answer the main question straight away",
                         "bad": "that don't get to the point in the first few lines",
                         "why": "AI assistants and busy visitors look for a direct answer at the top of the page.",
                         "do": "Start each page with 2–3 sentences saying exactly what you offer. Drafts are in the technical details."},
    "geo.question_headings": {"t": "Headings written as questions",
                              "bad": "with no headings written as questions",
                              "why": "People ask AI assistants questions; headings that match those questions get quoted.",
                              "do": "Turn some headings into the questions customers ask, e.g. 'How much does a go-kart cost?'."},
    "geo.faq": {"t": "Questions & answers section on key pages",
                "bad": "among your key pages without a questions & answers section",
                "why": "Short Q&As are one of the formats AI assistants quote most.",
                "do": "Add an FAQ to key pages. Draft questions are in the technical details."},
    "geo.sources_and_stats": {"t": "Facts and figures with sources",
                              "bad": "with claims but no facts or sources",
                              "why": "Specific numbers with a source make your content more believable and quotable.",
                              "do": "Back up claims with numbers and say where they come from."},
    "geo.authorship": {"t": "Articles show who wrote them and when",
                       "bad": "with an article but no author name or date",
                       "why": "Google and AI assistants trust content more when they know who wrote it and when.",
                       "do": "Show the author's name and the publish date on each article."},
    "geo.eeat_site": {"t": "Signs that a real, trustworthy business is behind the site",
                      "bad": "The site is missing some trust signals (such as an About page, contact details or a privacy policy).",
                      "why": "Visitors, Google and AI assistants trust sites more when they can see who runs them.",
                      "do": "Add the missing pages and show your phone number and address on the homepage."},
}

# NOT_CHECKED reasons in plain words (first match wins); the original reason stays in the technical details
_REASONS = [
    (r"not built yet", "This agent is still being built and will join in a later update."),
    (r"Search Console", "Needs access to your Google Search Console account (a free Google tool). Your developer can connect it."),
    (r"Analytics", "Needs access to your Google Analytics account."),
    (r"ANTHROPIC_API_KEY|manual AI mode|LLM|AI analysis|Could not generate prompts|Anthropic",
     "Needs the AI assistant to be switched on in Settings."),
    (r"BRAVE_API_KEY|Search provider", "Needs the web search service to be set up in Settings."),
    (r"PAGESPEED|PageSpeed API", "Needs Google's free speed-test service to be set up in Settings."),
    (r"Colab|Common Crawl|Authority table|inlinks|imported",
     "Needs the full list of linking websites, which comes from a periodic data import."),
    (r"[Cc]ompetitor", "Add your competitors' websites when you start an audit to unlock this."),
    (r"could not be verified|could be fetched|couldn't be fetched|network|unreachable|blocked|timed? ?out",
     "We couldn't reach the other websites needed for this check. It usually works on the next run."),
    (r"real-user|field data|CrUX|not enough (data|traffic)",
     "Google doesn't have enough visitor data for your site yet, so this can't be measured."),
    (r"[Nn]ot enough|[Nn]o .*found|snippets", "There wasn't enough public information to judge this."),
    (r"[Bb]udget", "This audit's spending limit was reached before this check ran."),
]


def plain_reason(reason: str | None) -> str:
    for rx, text in _REASONS:
        if reason and re.search(rx, reason):
            return text
    return "We couldn't check this automatically this time."


def agent_of(module: str) -> dict[str, str]:
    return AGENTS.get(module, {"name": module.replace("_", " ").title() + " Agent", "icon": "•", "who": DESIGNER, "job": ""})


def grade(score: float | None) -> dict[str, str]:
    if score is None:
        return {"label": "Not enough data", "sentence": "We couldn't measure enough of your site to give a score."}
    if score >= 90:
        return {"label": "Excellent", "sentence": "Your website is in great shape. Only small improvements are left."}
    if score >= 75:
        return {"label": "Good, with some fixes needed",
                "sentence": "The basics are in place, but several problems are holding your website back in Google."}
    if score >= 50:
        return {"label": "Needs work", "sentence": "Your website has a number of problems that make it harder to find on Google."}
    return {"label": "Needs a lot of work", "sentence": "Your website has serious problems that stop many customers from finding it."}


def _found(d: dict[str, Any], spec: dict[str, str], population: str | None) -> str:
    status = d["status"]
    if status == "PASS":
        return "No problems found."
    if status == "N/A":
        return d.get("reason") or "This doesn't apply to your website."
    if status == "NOT_CHECKED":
        return plain_reason(d.get("reason"))
    n, total = d.get("affected") or 0, d.get("total")
    if population in NOUNS and n:
        one, many = NOUNS[population]
        noun = one if n == 1 else many
        tail = f" (out of {total} checked)" if total else ""
        return f"We found {n} {noun} {spec['bad']}{tail}."
    return spec.get("bad") or d.get("summary") or ""


def plain_fields(d: dict[str, Any], population: str | None = None) -> dict[str, Any]:
    """Plain-language fields for one finding dict (as produced by reports.build.finding_dict)."""
    spec = P.get(d["check_id"], {})
    agent = agent_of(d["module"])
    return {
        "agent": agent["name"], "agent_icon": agent["icon"],
        "plain_title": spec.get("t") or d.get("title"),
        "plain_status": STATUS_WORDS.get(d["status"], d["status"]),
        "plain_icon": STATUS_ICONS.get(d["status"], ""),
        "plain_found": _found(d, spec, population),
        "plain_why": spec.get("why") or d.get("why") or "",
        "plain_do": spec.get("do") or d.get("fix") or "",
        "plain_who": spec.get("who") or agent["who"],
    }


def _things(n: int, what: str) -> str:
    return f"{n} thing{'' if n == 1 else 's'} {what}"


def agent_summaries(findings: list[dict[str, Any]], module_order: list[str]) -> list[dict[str, Any]]:
    """One entry per agent, in module order, with its checks and a one-line verdict."""
    by: dict[str, list[dict]] = {}
    for f in findings:
        by.setdefault(f["module"], []).append(f)
    out = []
    for key in module_order:
        fs = by.get(key)
        if not fs:
            continue
        a = agent_of(key)
        n = {s: sum(1 for f in fs if f["status"] == s) for s in ("FAIL", "WARNING", "PASS", "NOT_CHECKED", "N/A")}
        planned = all("not built yet" in (f.get("reason") or "") for f in fs)
        if planned:
            verdict, tone = "Coming in a later update", "NA"
        elif n["FAIL"]:
            verdict, tone = _things(n["FAIL"], "to fix") + (f", {n['WARNING']} to improve" if n["WARNING"] else ""), "FAIL"
        elif n["WARNING"]:
            verdict, tone = _things(n["WARNING"], "to improve"), "WARNING"
        elif n["PASS"]:
            verdict, tone = "All good", "PASS"
        else:
            verdict, tone = "Couldn't check this time", "NOT_CHECKED"
        order = {"FAIL": 0, "WARNING": 1, "PASS": 2, "NOT_CHECKED": 3, "N/A": 4}
        out.append({"key": key, "name": a["name"], "icon": a["icon"], "job": a["job"], "who": a["who"],
                    "verdict": verdict, "tone": tone, "counts": n, "planned": planned,
                    "findings": sorted(fs, key=lambda f: (order.get(f["status"], 9), -(f.get("priority") or 0)))})
    return out


def counts_sentence(counts: dict[str, int]) -> str:
    total = sum(counts.values())
    parts = [f"{counts.get('PASS', 0)} look good", f"{counts.get('FAIL', 0)} need fixing",
             f"{counts.get('WARNING', 0)} could be better"]
    if counts.get("NOT_CHECKED"):
        parts.append(f"{counts['NOT_CHECKED']} couldn't be checked yet")
    return f"Our agents ran {total} checks: " + ", ".join(parts) + "."
