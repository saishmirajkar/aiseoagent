"""Module 5: Image SEO (deterministic checks; vision alt text is added by the LLM layer)."""
from __future__ import annotations

import re
from collections import defaultdict

from app.modules.base import (
    AuditContext, Category, CheckModule, CheckResult, CheckSpec, FindingItem, Integration, Level, Need,
)
from app.crawler.urls import filename_of, host_of
from app.sitemodel.types import ImageAsset, ImageRec, PageData

H, M, L = Level.HIGH, Level.MED, Level.LOW
# CDNs that replace uploaded file names with hashes (the site owner cannot choose names)
PLATFORM_RENAMING_CDNS = {"framerusercontent.com": "Framer", "static.wixstatic.com": "Wix",
                          "images.ctfassets.net": "Contentful"}
LEGACY = {"jpeg", "png", "gif", "bmp"}
_GENERIC_NAME = re.compile(
    r"^(img|image|dsc|dscn|dcim|pic|picture|photo|screenshot|screen[ _-]?shot|untitled|unnamed|"
    r"whatsapp[ _-]image|pxl|mvimg|gopr|banner|header|default|placeholder|file|download)"
    r"[\s_\-()]*\d*([\s_\-()]*(copy|\d+|x\d+|scaled|\d+x\d+))*$", re.I)
_HASHY = re.compile(r"^[a-f0-9]{16,}$|^[A-Za-z0-9_-]{22,}$")
_NUMERIC = re.compile(r"^[\d\s_\-x]+$")


def non_descriptive_filename(filename: str) -> bool:
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    stem = re.sub(r"-\d+x\d+$", "", stem)  # WordPress size suffix
    if not stem:
        return False
    return bool(_GENERIC_NAME.match(stem) or _HASHY.match(stem) or _NUMERIC.match(stem))


def _tiny(img: ImageRec, a: ImageAsset | None) -> bool:
    """Tracking pixels / spacers are ignored."""
    dims = [img.width_attr, img.height_attr]
    if a is not None:
        dims += [a.intrinsic_w, a.intrinsic_h]
    return any(d is not None and d <= 2 for d in dims)


def group_by_image(pairs: list[tuple[PageData, ImageRec]], evidence, fix=None) -> list[FindingItem]:
    """One finding item per unique image file, listing every page it appears on.

    A logo with no alt in a site-wide header is one fix, not one problem per page.
    """
    groups: dict[str, list[tuple[PageData, ImageRec]]] = defaultdict(list)
    for p, i in pairs:
        groups[i.src].append((p, i))
    items = []
    for src, lst in groups.items():
        pages = list(dict.fromkeys(p.url for p, _ in lst))
        p0, i0 = lst[0]
        ev = {"src": src, **evidence(p0, i0), "pages": len(pages), "occurrences": len(lst)}
        items.append(FindingItem(pages[0], ev, generated_fix=fix(p0, i0) if fix else None, also_on=pages[1:]))
    items.sort(key=lambda it: -it.evidence["occurrences"])
    return items


def _display_w(img: ImageRec) -> int | None:
    return img.rendered_w or img.width_attr


class ImageSEO(CheckModule):
    key = "images"
    number = 5
    name = "Image SEO"
    category = Category.ONPAGE
    needs = frozenset({Need.PAGES, Need.IMAGES})
    integrations = frozenset({Integration.VISION})
    checks = (
        CheckSpec("img.alt_missing", "Images missing alt attribute", M, L,
                  "Alt text describes images to search engines and screen-reader users.",
                  "Add descriptive alt text (what the image shows, in context).", population="images",
                  missing_label="Alt text missing on {affected} image(s)"),
        CheckSpec("img.alt_empty", "Image links with empty alt (link has no text)", M, L,
                  "When an image is the only content of a link, its alt is the link's anchor text; empty alt "
                  "leaves the link with no name for search engines and screen readers.",
                  "Give the image an alt that describes the link target (e.g. \"Genesis TS22 kart\").",
                  population="images"),
        CheckSpec("img.filename", "Non-descriptive image file names", L, M,
                  "File names like IMG_1234.jpg give search engines no context.",
                  "Rename files to short descriptive slugs (e.g. red-leather-sofa.jpg) and update references.",
                  population="images"),
        CheckSpec("img.file_size", "Heavy image files", M, M,
                  "Large images slow page loads and hurt Largest Contentful Paint.",
                  "Compress and resize; serve WebP/AVIF at the displayed size.", population="images"),
        CheckSpec("img.oversized_dimensions", "Images much larger than their display size", M, M,
                  "Serving a 2000px image in a 400px slot wastes bandwidth.",
                  "Resize to ~2× the display width and use srcset/sizes for responsive images.",
                  population="images"),
        CheckSpec("img.legacy_format", "Images not in a modern format (WebP/AVIF)", L, M,
                  "WebP/AVIF are typically 25–50% smaller than JPEG/PNG at the same quality.",
                  "Convert to WebP or AVIF (or serve via <picture> with a JPEG/PNG fallback).",
                  population="images"),
        CheckSpec("img.missing_dimensions", "Images without width/height attributes", M, L,
                  "Without dimensions the browser cannot reserve space, causing layout shift (CLS).",
                  "Add width and height attributes matching the image's aspect ratio.", population="images"),
        CheckSpec("img.lazy_missing", "Below-the-fold images not lazy-loaded", L, L,
                  "Loading every image up front delays the content users see first.",
                  "Add loading=\"lazy\" to images below the fold.", population="images"),
        CheckSpec("img.lazy_above_fold", "Lazy-loading on the first/hero image", M, L,
                  "Lazy-loading the main above-the-fold image delays Largest Contentful Paint.",
                  "Remove loading=\"lazy\" from the first visible image (consider fetchpriority=\"high\").",
                  population="pages"),
        CheckSpec("img.broken", "Broken images", M, L,
                  "Broken images look unprofessional and waste requests.",
                  "Fix the image URL or remove the reference.", population="images"),
        CheckSpec("img.alt_generation", "AI-generated alt text suggestions", M, L,
                  "Ready-to-paste alt text for images that lack it.",
                  "Review and paste the generated alt text."),
    )

    async def run(self, ctx: AuditContext) -> list[CheckResult]:
        th = ctx.settings.thresholds
        assets = ctx.site.images
        pages: list[PageData] = ctx.site.html_pages()
        instances = [(p, img) for p in pages for img in p.images if not _tiny(img, assets.get(img.src))]
        n_uniq = len({i.src for _, i in instances})
        out: list[CheckResult] = []

        out.append(self.by_population(
            "img.alt_missing",
            group_by_image([(p, i) for p, i in instances if i.alt is None], lambda p, i: {"filename": i.filename}),
            n_uniq, summary="Images with no alt attribute (unique image files)"))

        # Whether a non-linked image with alt="" is decorative is a judgment call (vision model,
        # LLM layer). Deterministically we only flag image links that end up with no anchor text.
        linked = [(p, i) for p, i in instances if i.in_link and i.link_text_empty]
        empty = group_by_image([(p, i) for p, i in linked if i.alt is not None and not i.alt.strip()],
                               lambda p, i: {"alt": i.alt})
        out.append(self.by_population("img.alt_empty", empty, len({i.src for _, i in linked}), fail=False,
                                      summary="Image-only links whose image has empty alt"))

        # per unique image file
        uses: dict[str, list[str]] = defaultdict(list)
        for p, i in instances:
            if p.url not in uses[i.src]:
                uses[i.src].append(p.url)
        uniq = list(uses)
        platform_srcs = {s: PLATFORM_RENAMING_CDNS[host_of(s)] for s in uniq if host_of(s) in PLATFORM_RENAMING_CDNS}
        own = [s for s in uniq if s not in platform_srcs]
        fn = [FindingItem(uses[s][0], {"src": s, "filename": filename_of(s), "pages": len(uses[s])},
                          also_on=uses[s][1:])
              for s in own if non_descriptive_filename(filename_of(s))]
        platforms = sorted(set(platform_srcs.values()))
        if not own and platform_srcs:
            out.append(self.not_applicable(
                "img.filename", f"Image file names are generated by the site platform ({', '.join(platforms)}) "
                                "and cannot be renamed there; descriptive alt text matters more"))
        else:
            r = self.by_population("img.filename", fn, len(own), fail=False,
                                   summary="Generic or random image file names")
            if platform_srcs:
                r.summary += (f" ({len(platform_srcs)} image(s) hosted by {', '.join(platforms)} excluded: "
                              "the platform generates their names)")
            out.append(r)

        probed = [s for s in uniq if s in assets and assets[s].status == 200 and assets[s].bytes is not None]
        if not probed:
            reason = "No images could be downloaded for measurement" if uniq else "No images found"
            out.append(self.not_checked("img.file_size", reason) if uniq else self.not_applicable("img.file_size", reason))
        else:
            heavy, any_fail = [], False
            for s in probed:
                a = assets[s]
                kb = a.bytes / 1024
                if a.format == "svg":
                    continue
                if kb > th.image_warn_kb:
                    any_fail = any_fail or kb > th.image_fail_kb
                    heavy.append(FindingItem(uses[s][0], {"src": s, "kb": round(kb), "format": a.format,
                                                          "intrinsic": [a.intrinsic_w, a.intrinsic_h],
                                                          "exact_size": a.bytes_exact, "pages": len(uses[s])},
                                             also_on=uses[s][1:]))
            heavy.sort(key=lambda i: -i.evidence["kb"])
            out.append(self.by_population("img.file_size", heavy, len(probed), fail=any_fail,
                                          summary=f"Images over {th.image_warn_kb} KB (FAIL above {th.image_fail_kb} KB)"))

        # oversized vs display
        measured = set()
        over_pairs = []
        for p, i in instances:
            a = assets.get(i.src)
            dw = _display_w(i)
            if a is None or not a.intrinsic_w or not dw or a.format == "svg":
                continue
            measured.add(i.src)
            if a.intrinsic_w > th.image_oversize_ratio * dw and a.intrinsic_w > 400 and not i.has_srcset:
                over_pairs.append((p, i))

        def _over_ev(p, i):
            return {"intrinsic_w": assets[i.src].intrinsic_w, "display_w": _display_w(i),
                    "display_source": "rendered" if i.rendered_w else "width attribute"}

        def _over_fix(p, i):
            target = min(assets[i.src].intrinsic_w, _display_w(i) * 2)
            return {"type": "resize", "target_width": target,
                    "note": f"Resize to {target}px wide (2× display) or add srcset"}
        over = group_by_image(over_pairs, _over_ev, _over_fix)
        if not measured:
            out.append(self.not_checked("img.oversized_dimensions",
                                        "Display sizes unknown: no rendered measurements or width attributes"))
        else:
            r = self.by_population("img.oversized_dimensions", over, len(measured), fail=False,
                                   summary=f"Images over {th.image_oversize_ratio}× their display width")
            r.sources = ["Crawl", "Headless render sample (display sizes)"]
            out.append(r)

        # formats
        fmt_known = [s for s in probed if assets[s].format]
        legacy = []
        for s in fmt_known:
            a = assets[s]
            if a.format in LEGACY and (a.bytes or 0) / 1024 >= th.image_legacy_min_kb:
                inst = [i for p, i in instances if i.src == s]
                if inst and all(i.modern_source for i in inst):
                    continue
                legacy.append(FindingItem(uses[s][0], {"src": s, "format": a.format, "kb": round((a.bytes or 0) / 1024),
                                                       "pages": len(uses[s])},
                                          generated_fix={"type": "command",
                                                         "code": f"cwebp -q 80 input.{a.format} -o output.webp"},
                                          also_on=uses[s][1:]))
        if not fmt_known:
            out.append(self.not_checked("img.legacy_format", "No image formats could be detected"))
        else:
            out.append(self.by_population("img.legacy_format", legacy, len(fmt_known), fail=False,
                                          summary="JPEG/PNG/GIF images that could be WebP/AVIF"))

        def _dim_fix(p, i):
            a = assets.get(i.src)
            if a and a.intrinsic_w and a.intrinsic_h:
                return {"type": "html_attrs", "code": f'width="{a.intrinsic_w}" height="{a.intrinsic_h}"'}
            return None
        nodim = group_by_image([(p, i) for p, i in instances if i.width_attr is None or i.height_attr is None],
                               lambda p, i: {"width": i.width_attr, "height": i.height_attr}, _dim_fix)
        out.append(self.by_population("img.missing_dimensions", nodim, n_uniq, fail=False,
                                      summary="Images without explicit width and height"))

        lazy_pairs, lazy_top = [], []
        for p in pages:
            imgs = [i for i in p.images if not _tiny(i, assets.get(i.src))]
            for idx, i in enumerate(imgs):
                below = (i.above_fold is False) if i.above_fold is not None else idx >= 3
                lazy = (i.loading or "").lower() == "lazy"
                if below and not lazy:
                    lazy_pairs.append((p, i))
            if imgs:
                first = imgs[0]
                top = first.above_fold if first.above_fold is not None else True
                if top and (first.loading or "").lower() == "lazy":
                    lazy_top.append(FindingItem(p.url, {"src": first.src, "loading": first.loading}))
        lazy_missing = group_by_image(lazy_pairs, lambda p, i: {
            "fold_source": "rendered" if i.above_fold is not None else "position heuristic (4th+ image)"})
        out.append(self.by_population("img.lazy_missing", lazy_missing, n_uniq, fail=False,
                                      summary="Images likely below the fold without loading=\"lazy\""))
        out.append(self.by_population("img.lazy_above_fold", lazy_top, len([p for p in pages if p.images]),
                                      fail=False, summary="First image on the page is lazy-loaded"))

        checked = [s for s in uniq if s in assets and (assets[s].status is not None)]
        broken = [FindingItem(uses[s][0], {"src": s, "status": assets[s].status, "pages": len(uses[s])},
                              also_on=uses[s][1:])
                  for s in checked if assets[s].status and assets[s].status >= 400 and assets[s].status not in (401, 403, 429)]
        out.append(self.by_population("img.broken", broken, len(checked), summary="Image URLs return errors")
                   if checked else self.not_applicable("img.broken", "No images checked"))

        out.append(self.not_checked("img.alt_generation",
                                    "Vision-model alt text is part of the LLM layer, which is not built yet"))
        return out
