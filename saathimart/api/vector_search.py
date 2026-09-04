"""
Vector Search using Qdrant + Sentence Transformers for semantic product search.

This module enables semantic search that understands meaning, not just exact matches:
- "chiya" matches "tea"
- "mobile" matches "phone"
- Typos are handled automatically

For Nepali language support, we use a multilingual model trained on multiple languages.
"""
import frappe
from frappe.utils import now_datetime


# Lazy-loaded model and client
_model = None
_client = None


def get_model():
    """Get or create the Sentence Transformers model."""
    global _model
    if _model is not None:
        return _model

    try:
        from sentence_transformers import SentenceTransformer
        # Use multilingual model that supports Nepali
        # Based on paraphrase-multilingual-MiniLM-L12-v2
        _model = SentenceTransformer("sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")
        frappe.log_error("Vector search model loaded successfully", "vector_search")
    except ImportError:
        frappe.log_error("sentence-transformers not installed. Install with: pip install sentence-transformers", "vector_search")
        _model = None
    except Exception as e:
        frappe.log_error(f"Failed to load vector search model: {e}", "vector_search")
        _model = None

    return _model


def get_client():
    """Get or create the Qdrant client."""
    global _client
    if _client is not None:
        return _client

    try:
        from qdrant_client import QdrantClient
        host = frappe.conf.get("qdrant_host", "localhost")
        port = frappe.conf.get("qdrant_port", 6333)

        _client = QdrantClient(host=host, port=port)
        # Test connection
        _client.get_collections()
        frappe.log_error("Qdrant client connected successfully", "vector_search")
    except ImportError:
        frappe.log_error("qdrant-client not installed. Install with: pip install qdrant-client", "vector_search")
        _client = None
    except Exception as e:
        frappe.log_error(f"Failed to connect to Qdrant: {e}", "vector_search")
        _client = None

    return _client


def get_vector_size():
    """Get the vector size for the current model."""
    model = get_model()
    if model:
        return model.get_sentence_embedding_dimension()
    return 384  # Default for MiniLM models


def create_collection_if_not_exists():
    """Create the products collection in Qdrant if it doesn't exist."""
    client = get_client()
    if not client:
        return False

    try:
        from qdrant_client.models import VectorParams, Distance, CollectionStatus

        collection_name = "products"
        vector_size = get_vector_size()

        # Check if collection exists
        collections = client.get_collections()
        collection_exists = any(c.name == collection_name for c in collections.collections)

        if not collection_exists:
            client.recreate_collection(
                collection_name=collection_name,
                vectors_config=VectorParams(size=vector_size, distance=Distance.COSINE)
            )
            frappe.log_error(f"Created Qdrant collection: {collection_name}", "vector_search")

        return True
    except Exception as e:
        frappe.log_error(f"Failed to create Qdrant collection: {e}", "vector_search")
        return False


def index_product(product_id, product_name, description="", category="", brand=""):
    """Index a single product in Qdrant for semantic search.

    The embedding combines product name, description, category, and brand
    to enable better semantic matching.
    """
    model = get_model()
    client = get_client()

    if not model or not client:
        return False

    try:
        # Create combined text for embedding
        text = f"{product_name} {description} {category or ''} {brand or ''}"
        embedding = model.encode(text).tolist()

        # Upsert to Qdrant
        client.upsert(
            collection_name="products",
            points=[{
                "id": product_id,
                "vector": embedding,
                "payload": {
                    "id": product_id,
                    "product_name": product_name,
                    "description": description,
                    "category": category,
                    "brand": brand,
                }
            }]
        )
        frappe.db.set_value("Product", product_id, {
            "indexed_at": now_datetime(),
        }, update_modified=False)
        frappe.db.commit()
        return True
    except Exception as e:
        frappe.log_error(f"Failed to index product {product_id}: {e}", "vector_search")
        return False


def index_all_products():
    """Index all active products in Qdrant.

    Run this once to populate the initial vector index.
    """
    products = frappe.get_all(
        "Product",
        filters={"status": "Active"},
        fields=["name", "product_name", "description", "category", "brand"]
    )

    indexed = 0
    failed = 0

    for product in products:
        if index_product(
            product_id=product.name,
            product_name=product.product_name,
            description=product.description or "",
            category=product.category or "",
            brand=product.brand or ""
        ):
            indexed += 1
        else:
            failed += 1

    frappe.log_error(
        f"Indexed {indexed} products, {failed} failed", "vector_search"
    )
    return {"indexed": indexed, "failed": failed}


def semantic_search(query, limit=10):
    """Search products semantically using Qdrant.

    Returns product IDs ordered by relevance to the query.
    Falls back to keyword search if Qdrant is unavailable.
    """
    model = get_model()
    client = get_client()

    if not query or not query.strip():
        return []

    if not model or not client:
        # Fallback to keyword search
        return keyword_search(query, limit)

    try:
        # Encode query to vector
        query_vector = model.encode(query).tolist()

        # Search Qdrant
        results = client.search(
            collection_name="products",
            query_vector=query_vector,
            limit=limit,
            score_threshold=0.3  # Minimum similarity score
        )

        # Extract product IDs
        product_ids = [r.payload.get("id") for r in results if r.payload.get("id")]

        # If not enough results, fall back to keyword search
        if len(product_ids) < limit:
            keyword_results = keyword_search(query, limit - len(product_ids))
            product_ids.extend([pid for pid in keyword_results if pid not in product_ids])

        return product_ids[:limit]
    except Exception as e:
        frappe.log_error(f"Vector search failed: {e}", "vector_search")
        # Fallback to keyword search on error
        return keyword_search(query, limit)


def keyword_search(query, limit=10):
    """Fallback keyword search using MariaDB.

    This is the existing search logic when Qdrant is unavailable.
    """
    if not query:
        return []

    # Use the existing search_products logic for fallback
    from saathimart.api.search import search_products

    try:
        result = search_products(query=query, page=1, page_size=limit)
        return [r.get("name") for r in result.get("results", [])]
    except Exception as e:
        frappe.log_error(f"Keyword search failed: {e}", "vector_search")
        return []


def get_vector_stats():
    """Get statistics about the vector index."""
    client = get_client()
    if not client:
        return {"status": "unavailable", "message": "Qdrant not connected"}

    try:
        collection_info = client.get_collection("products")
        return {
            "status": "ready",
            "vectors_count": collection_info.points_count,
            "vector_size": collection_info.config.params.vectors.size,
            "indexed_at": frappe.db.get_value("Product", {"indexed_at": ("is", "set")}, "max(indexed_at)")
        }
    except Exception as e:
        return {"status": "error", "message": str(e)}


def health_check():
    """Health check for vector search service."""
    model = get_model()
    client = get_client()

    if not model:
        return {"status": "error", "component": "model", "message": "Model not loaded"}

    if not client:
        return {"status": "error", "component": "qdrant", "message": "Qdrant not connected"}

    try:
        collections = client.get_collections()
        return {
            "status": "healthy",
            "qdrant": "connected",
            "collections": [c.name for c in collections.collections],
            "model": "loaded"
        }
    except Exception as e:
        return {"status": "error", "component": "qdrant", "message": str(e)}
