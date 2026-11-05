"""Module 5: Image SEO (deterministic checks; vision alt text is added by the LLM layer)."""
from __future__ import annotations

import re
from collections import defaultdict

from app.modules.base import (
    AuditContext, Category, CheckModule, CheckResult, CheckSpec, FindingItem, Integration, Level, Need,
)
from app.crawler.urls import filename_of
from app.sitemodel.types import ImageAsset, ImageRec, PageData

H, M, L = Level.HIGH, Level.MED, Level.LOW
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
        CheckSpec("img.alt_empty", "Empty alt on meaningful images", L, L,
                  "Empty alt marks an image as decorative; on linked or large content images it hides meaning.",
                  "Describe content images and image links; keep alt=\"\" only for decorative images.",
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
        n_inst = len(instances)
        out: list[CheckResult] = []

        out.append(self.by_population(
            "img.alt_missing",
            [FindingItem(p.url, {"src": i.src, "filename": i.filename}) for p, i in instances if i.alt is None],
            n_inst, summary="Images with no alt attribute"))

        empty = []
        for p, i in instances:
            if i.alt is None or i.alt.strip():
                continue
            a = assets.get(i.src)
            large = a is not None and (a.intrinsic_w or 0) >= 300
            if i.in_link or large:
                empty.append(FindingItem(p.url, {"src": i.src, "in_link": i.in_link,
                                                 "intrinsic_w": a.intrinsic_w if a else None}))
        out.append(self.by_population("img.alt_empty", empty, n_inst, fail=False,
                                      summary="Linked or large images with empty alt"))

        # per unique image file
        uses: dict[str, list[str]] = defaultdict(list)
        for p, i in instances:
            if p.url not in uses[i.src]:
                uses[i.src].append(p.url)
        uniq = list(uses)
        fn = [FindingItem(uses[s][0], {"src": s, "filename": filename_of(s), "used_on": uses[s][:5]})
              for s in uniq if non_descriptive_filename(filename_of(s))]
        out.append(self.by_population("img.filename", fn, len(uniq), fail=False,
                                      summary="Generic or random image file names"))

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
                                                          "exact_size": a.bytes_exact, "used_on": uses[s][:5]}))
            heavy.sort(key=lambda i: -i.evidence["kb"])
            out.append(self.by_population("img.file_size", heavy, len(probed), fail=any_fail,
                                          summary=f"Images over {th.image_warn_kb} KB (FAIL above {th.image_fail_kb} KB)"))

        # oversized vs display
        measured = []
        over = []
        for p, i in instances:
            a = assets.get(i.src)
            dw = _display_w(i)
            if a is None or not a.intrinsic_w or not dw or a.format == "svg":
                continue
            measured.append(i)
            if a.intrinsic_w > th.image_oversize_ratio * dw and a.intrinsic_w > 400 and not i.has_srcset:
                target = min(a.intrinsic_w, dw * 2)
                over.append(FindingItem(p.url, {"src": i.src, "intrinsic_w": a.intrinsic_w, "display_w": dw,
                                                "display_source": "rendered" if i.rendered_w else "width attribute"},
                                        generated_fix={"type": "resize", "target_width": target,
                                                       "note": f"Resize to {target}px wide (2× display) or add srcset"}))
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
                legacy.append(FindingItem(uses[s][0], {"src": s, "format": a.format, "kb": round((a.bytes or 0) / 1024)},
                                          generated_fix={"type": "command",
                                                         "code": f"cwebp -q 80 input.{a.format} -o output.webp"}))
        if not fmt_known:
            out.append(self.not_checked("img.legacy_format", "No image formats could be detected"))
        else:
            out.append(self.by_population("img.legacy_format", legacy, len(fmt_known), fail=False,
                                          summary="JPEG/PNG/GIF images that could be WebP/AVIF"))

        nodim = []
        for p, i in instances:
            if i.width_attr is None or i.height_attr is None:
                a = assets.get(i.src)
                fix = None
                if a and a.intrinsic_w and a.intrinsic_h:
                    fix = {"type": "html_attrs", "code": f'width="{a.intrinsic_w}" height="{a.intrinsic_h}"'}
                nodim.append(FindingItem(p.url, {"src": i.src, "width": i.width_attr, "height": i.height_attr},
                                         generated_fix=fix))
        out.append(self.by_population("img.missing_dimensions", nodim, n_inst, fail=False,
                                      summary="Images without explicit width and height"))

        lazy_missing, lazy_top = [], []
        for p in pages:
            imgs = [i for i in p.images if not _tiny(i, assets.get(i.src))]
            for idx, i in enumerate(imgs):
                below = (i.above_fold is False) if i.above_fold is not None else idx >= 3
                lazy = (i.loading or "").lower() == "lazy"
                if below and not lazy:
                    lazy_missing.append(FindingItem(p.url, {"src": i.src, "position": idx,
                                                            "fold_source": "rendered" if i.above_fold is not None else "position heuristic"}))
            if imgs:
                first = imgs[0]
                top = first.above_fold if first.above_fold is not None else True
                if top and (first.loading or "").lower() == "lazy":
                    lazy_top.append(FindingItem(p.url, {"src": first.src, "loading": first.loading}))
        out.append(self.by_population("img.lazy_missing", lazy_missing, n_inst, fail=False,
                                      summary="Images likely below the fold without loading=\"lazy\""))
        out.append(self.by_population("img.lazy_above_fold", lazy_top, len([p for p in pages if p.images]),
                                      fail=False, summary="First image on the page is lazy-loaded"))

        checked = [s for s in uniq if s in assets and (assets[s].status is not None)]
        broken = [FindingItem(uses[s][0], {"src": s, "status": assets[s].status, "used_on": uses[s][:5]})
                  for s in checked if assets[s].status and assets[s].status >= 400 and assets[s].status not in (401, 403, 429)]
        out.append(self.by_population("img.broken", broken, len(checked), summary="Image URLs return errors")
                   if checked else self.not_applicable("img.broken", "No images checked"))

        out.append(self.not_checked("img.alt_generation",
                                    "Vision-model alt text is part of the LLM layer, which is not built yet"))
        return out
