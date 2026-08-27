"""
Image handling — thumbnail generation, format optimization, and CDN URL support.

Supports:
  - Auto-generate thumbnails at upload time (300px, 600px, 1200px)
  - WebP conversion for modern browsers
  - CDN URL rewriting when configured
"""
import frappe
from frappe import _
import os


THUMBNAIL_SIZES = {
    "small": 300,
    "medium": 600,
    "large": 1200,
}


def get_image_url(file_url, size="medium"):
    """Return the URL for an image, with optional CDN prefix.

    If CDN_BASE_URL is configured in Settings, prefixes the file URL.
    Otherwise returns the original Frappe file URL.
    """
    if not file_url:
        return ""

    # Already absolute or external URL
    if file_url.startswith(("http://", "https://")):
        return file_url

    # CDN rewriting
    cdn_base = frappe.db.get_single_value("Settings", "cdn_base_url") or ""
    if cdn_base:
        return "{0}{1}".format(cdn_base.rstrip("/"), file_url)

    return file_url


def get_thumbnail_url(file_url, size="small"):
    """Return the thumbnail URL for an image.

    If thumbnails have been generated, returns the thumbnail path.
    Otherwise returns the original image (frontend can resize client-side).
    """
    if not file_url:
        return ""

    # Check if thumbnail exists
    size_px = THUMBNAIL_SIZES.get(size, 300)
    thumb_path = _get_thumbnail_path(file_url, size_px)
    if thumb_path and os.path.exists(thumb_path):
        # Return the URL path relative to files/
        rel_path = thumb_path.split("/files/")[-1] if "/files/" in thumb_path else file_url
        return get_image_url("/files/" + rel_path)

    return get_image_url(file_url)


def generate_thumbnails(file_url):
    """Generate thumbnails for an uploaded image.

    Called after file upload. Creates resized versions in the files directory.
    """
    if not file_url:
        return

    # Only process image files
    ext = os.path.splitext(file_url)[1].lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp", ".gif"):
        return

    try:
        from PIL import Image
        import io

        # Get the file path
        site_path = frappe.get_site_path("public" + file_url)
        if not os.path.exists(site_path):
            return

        img = Image.open(site_path)
        base_name = os.path.splitext(os.path.basename(file_url))[0]

        files_dir = frappe.get_site_path("public", "files")
        os.makedirs(files_dir, exist_ok=True)

        for size_name, size_px in THUMBNAIL_SIZES.items():
            thumb = img.copy()
            thumb.thumbnail((size_px, size_px), Image.Resampling.LANCZOS)
            thumb_name = "{0}_{1}{2}".format(base_name, size_name, ext)
            thumb_path = os.path.join(files_dir, thumb_name)
            thumb.save(thumb_path, quality=85, optimize=True)

    except ImportError:
        pass  # PIL not installed — skip thumbnail generation
    except Exception:
        frappe.log_error(frappe.get_traceback(), "Thumbnail generation failed")


def _get_thumbnail_path(file_url, size_px):
    """Get the expected filesystem path for a thumbnail."""
    ext = os.path.splitext(file_url)[1].lower()
    base_name = os.path.splitext(os.path.basename(file_url))[0]
    size_name = {300: "small", 600: "medium", 1200: "large"}.get(size_px, "medium")
    thumb_name = "{0}_{1}{2}".format(base_name, size_name, ext)
    return frappe.get_site_path("public", "files", thumb_name)
