from app.modules.base import Status
from app.modules.m03_meta_tags import MetaTags
from app.modules.m05_images import ImageSEO, non_descriptive_filename

from tests.unit.helpers import BASE, asset, ctx, img, link, page, run, site, urls

META = MetaTags()
IMG = ImageSEO()


# ------------------------------------------------------------------ meta tags
def test_meta_all_good():
    s = site([page("/", links=[link("/a")]), page("/a", title="Another reasonable and unique page title",
                                                     meta_description="e" * 140)])
    r = run(META, ctx(s))
    for cid in ("meta.title_missing", "meta.title_multiple", "meta.title_duplicate", "meta.title_too_long",
                "meta.title_too_short", "meta.description_missing", "meta.description_multiple",
                "meta.description_duplicate", "meta.description_length", "meta.social_tags"):
        assert r[cid].status == Status.PASS, cid
    assert r["meta.keyword_in_title"].status == Status.NOT_CHECKED


def test_meta_problems():
    pages = [
        page("/", links=[link(f"/{x}") for x in "abcdef"], title="Home page title that is unique enough"),
        page("/a", title=None),
        page("/b", title="Dup title that is long enough here", title_count=2),
        page("/c", title="Dup title that is long enough here"),
        page("/d", title="W" * 45),  # within 60 chars but too wide in pixels
        page("/e", title="Short", meta_description=None),
        page("/f", meta_description="tiny", description_count=2, og={}, title="Page F has its own unique title"),
    ]
    r = run(META, ctx(site(pages)))
    assert urls(r["meta.title_missing"]) == {BASE + "/a"}
    assert urls(r["meta.title_multiple"]) == {BASE + "/b"}
    assert urls(r["meta.title_duplicate"]) == {BASE + "/b", BASE + "/c"}
    assert r["meta.title_duplicate"].status == Status.FAIL
    long_ = r["meta.title_too_long"]
    assert urls(long_) == {BASE + "/d"} and long_.items[0].evidence["pixels_est"] > 580
    assert urls(r["meta.title_too_short"]) == {BASE + "/e"}
    assert urls(r["meta.description_missing"]) == {BASE + "/e"}
    assert urls(r["meta.description_multiple"]) == {BASE + "/f"}
    assert urls(r["meta.description_length"]) == {BASE + "/f"}
    assert r["meta.description_duplicate"].status == Status.WARNING  # most pages share "d"*140
    social = r["meta.social_tags"]
    assert BASE + "/f" in urls(social) and "og:image" in social.items[0].generated_fix["code"]


def test_meta_ignores_noindex_and_canonicalised_duplicates():
    s = site([page("/", links=[link("/a"), link("/b")]),
              page("/a", title="Same title for both of these pages", meta_robots="noindex"),
              page("/b", title="Same title for both of these pages", canonical=BASE + "/", canonicals=[BASE + "/"])])
    r = run(META, ctx(s))
    assert r["meta.title_duplicate"].status == Status.PASS


# ------------------------------------------------------------------ images
def test_filename_heuristic():
    for bad in ("IMG_1234.jpg", "DSC00012.JPG", "image1.png", "screenshot-2.png", "1234567.jpg",
                "a3f9c0e1b2d4e5f6a7b8.webp", "photo-1024x768.jpg", "unnamed.png"):
        assert non_descriptive_filename(bad), bad
    for good in ("red-leather-sofa.jpg", "team-photo-2024.png", "logo.svg", "acme-widget-blue.webp"):
        assert not non_descriptive_filename(good), good


def test_image_checks():
    imgs = [
        img("/i/IMG_1234.jpg", alt=None, width_attr=400, height_attr=None, loading="lazy"),
        img("/i/hero.webp", alt="", in_link=True, link_text_empty=True),
        img("/i/deco.webp", alt=""),                                  # decorative: not flagged
        img("/i/named.webp", alt="", in_link=True, link_text_empty=False),  # link has text: fine
        img("/i/ok.webp"), img("/i/ok2.webp"),
        img("/i/big.png", rendered_w=300),
        img("/i/pixel.gif", alt=None, width_attr=1, height_attr=1),
    ]
    assets = {
        BASE + "/i/IMG_1234.jpg": asset("/i/IMG_1234.jpg", bytes=600_000, format="jpeg", intrinsic_w=2400, intrinsic_h=1600),
        BASE + "/i/hero.webp": asset("/i/hero.webp", bytes=40_000, format="webp", intrinsic_w=180, intrinsic_h=90),
        BASE + "/i/ok.webp": asset("/i/ok.webp", bytes=20_000, format="webp", intrinsic_w=100, intrinsic_h=100),
        BASE + "/i/ok2.webp": asset("/i/ok2.webp", status=404),
        BASE + "/i/big.png": asset("/i/big.png", bytes=200_000, format="png", intrinsic_w=1600, intrinsic_h=900),
        BASE + "/i/pixel.gif": asset("/i/pixel.gif", bytes=43, format="gif", intrinsic_w=1, intrinsic_h=1),
    }
    r = run(IMG, ctx(site([page("/", images=imgs)], images=assets)))
    assert r["img.alt_missing"].affected == 1  # tracking pixel ignored
    assert r["img.alt_empty"].status == Status.WARNING and urls(r["img.alt_empty"]) == {BASE + "/"}
    assert r["img.alt_empty"].affected == 1 and "hero.webp" in str(r["img.alt_empty"].items)
    assert "IMG_1234.jpg" in str(r["img.filename"].items)
    fs = r["img.file_size"]
    assert fs.status == Status.FAIL and fs.affected == 2 and fs.items[0].evidence["kb"] > 500
    over = r["img.oversized_dimensions"]
    assert over.affected == 2 and all(i.generated_fix["target_width"] for i in over.items)
    assert {i.evidence["format"] for i in r["img.legacy_format"].items} == {"jpeg", "png"}
    md = r["img.missing_dimensions"]
    assert md.affected == 1 and md.items[0].generated_fix["code"] == 'width="2400" height="1600"'
    assert r["img.lazy_above_fold"].status == Status.WARNING
    assert r["img.lazy_missing"].status == Status.WARNING  # 4th+ images not lazy (position heuristic)
    assert "ok2.webp" in str(r["img.broken"].items)
    assert r["img.alt_generation"].status == Status.NOT_CHECKED


def test_images_none():
    r = run(IMG, ctx(site([page("/")])))
    assert r["img.alt_missing"].status == Status.NOT_APPLICABLE
    assert r["img.file_size"].status == Status.NOT_APPLICABLE


def test_platform_generated_filenames_not_blamed():
    fr = "https://framerusercontent.com/images/5YNvxTDyGOu7IYItPcmRGgKeoyo.png"
    s = site([page("/", images=[img(fr)])], images={fr: asset(fr, bytes=1000, format="png")})
    r = run(IMG, ctx(s))
    assert r["img.filename"].status == Status.NOT_APPLICABLE and "Framer" in r["img.filename"].reason
    mixed = site([page("/", images=[img(fr), img("/i/IMG_0001.jpg")])])
    r2 = run(IMG, ctx(mixed))
    assert r2["img.filename"].affected == 1 and "Framer" in r2["img.filename"].summary


def test_shared_image_is_one_finding_listing_all_pages():
    logo = dict(alt=None, in_link=True, link_text_empty=True)
    pages = [page("/", images=[img("/logo.png", **logo)], links=[link("/a"), link("/b")]),
             page("/a", images=[img("/logo.png", **logo)]), page("/b", images=[img("/logo.png", **logo)])]
    r = run(IMG, ctx(site(pages)))["img.alt_missing"]
    assert r.affected == 1 and len(r.items) == 1
    item = r.items[0]
    assert item.evidence["pages"] == 3 and {item.url, *item.also_on} == {BASE + "/", BASE + "/a", BASE + "/b"}
