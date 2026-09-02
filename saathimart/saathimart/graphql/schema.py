"""GraphQL Schema for Saathimart."""

import graphene
from graphene import relay, ObjectType
from graphene_frappe import FrappeObjectType
from saathimart.api.products import list_products, get_product


class Product(FrappeObjectType):
    """GraphQL type for Product (from tabProduct)."""

    class Meta:
        name = "Product"
        model_name = "Product"

    # Add custom fields
    price = graphene.Float()
    in_stock = graphene.Boolean()
    vendor = graphene.String()


class Category(FrappeObjectType):
    """GraphQL type for Category."""

    class Meta:
        name = "Category"
        model_name = "Category"


class Brand(FrappeObjectType):
    """GraphQL type for Brand."""

    class Meta:
        name = "Brand"
        model_name = "Brand"


class Query(ObjectType):
    """Root query for Saathimart GraphQL API."""

    # Products
    products = graphene.List(
        Product,
        page=graphene.Int(default_value=1),
        page_size=graphene.Int(default_value=20),
        category=graphene.String(),
        search=graphene.String(),
        sort=graphene.String(),
        in_stock=graphene.Boolean(),
        min_price=graphene.Float(),
        max_price=graphene.Float(),
        brand=graphene.String(),
        lat=graphene.Float(),
        lng=graphene.Float(),
        session_id=graphene.String(),
    )
    
    product = graphene.Field(
        Product,
        slug=graphene.String(required=True),
    )

    # Categories
    categories = graphene.List(Category)
    
    # Brands
    brands = graphene.List(Brand)

    def resolve_products(
        self,
        info,
        page=1,
        page_size=20,
        category=None,
        search=None,
        sort=None,
        in_stock=None,
        min_price=None,
        max_price=None,
        brand=None,
        lat=None,
        lng=None,
        session_id=None,
    ):
        """Resolve products list."""
        result = list_products(
            page=page,
            page_size=page_size,
            category=category,
            search=search,
            sort=sort,
            in_stock=in_stock,
            min_price=min_price,
            max_price=max_price,
            brand=brand,
            lat=lat,
            lng=lng,
            session_id=session_id,
        )
        return result.get("items", [])

    def resolve_product(self, info, slug):
        """Resolve single product by slug."""
        from saathimart.api.products import get_product as _get_product
        try:
            result = _get_product(slug)
            return result
        except Exception:
            return None

    def resolve_categories(self, info):
        """Resolve all categories."""
        from saathimart.api.products import list_categories
        return list_categories()

    def resolve_brands(self, info):
        """Resolve all brands."""
        from saathimart.api.products import list_brands
        return list_brands()


schema = graphene.Schema(query=Query)
