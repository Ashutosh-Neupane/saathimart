"""
Multi-Warehouse System — Test Suite
Run: bench --site <site> run-tests --module saathimart.tests.test_multi_warehouse
"""
import unittest
import frappe
from frappe.utils import flt
from saathimart.api.warehouses import (
    _haversine_km, get_vendor_warehouses, get_default_warehouse,
    find_nearest_warehouse, get_stock_by_warehouse,
)
from saathimart.api.stock import get_or_create, _row_name


class TestHaversine(unittest.TestCase):
    def test_same_point_is_zero(self):
        self.assertEqual(_haversine_km(27.7172, 85.3240, 27.7172, 85.3240), 0.0)

    def test_known_distance(self):
        d = _haversine_km(27.7172, 85.3240, 28.2096, 83.9856)
        self.assertGreater(d, 130)
        self.assertLess(d, 160)

    def test_symmetric(self):
        d1 = _haversine_km(27.7172, 85.3240, 28.2096, 83.9856)
        d2 = _haversine_km(28.2096, 83.9856, 27.7172, 85.3240)
        self.assertAlmostEqual(d1, d2, places=2)


class TestVendorWarehouseCRUD(unittest.TestCase):
    VENDOR = "WH Test Vendor CRUD"
    PRODUCT = "WH Test Product CRUD"

    @classmethod
    def setUpClass(cls):
        frappe.set_user("Administrator")
        if not frappe.db.exists("Vendor", {"vendor_name": cls.VENDOR}):
            v = frappe.new_doc("Vendor")
            v.vendor_name = cls.VENDOR
            v.slug = "wh-test-crud"
            v.status = "Active"
            v.default_warehouse = "Default Store"
            v.flags.ignore_links = True
            v.insert(ignore_permissions=True)
            frappe.db.commit()
        cls.vendor_name = frappe.db.get_value("Vendor", {"vendor_name": cls.VENDOR}, "name")

        if not frappe.db.exists("Product", {"product_name": cls.PRODUCT}):
            p = frappe.new_doc("Product")
            p.product_name = cls.PRODUCT
            p.slug = "wh-test-prod-crud"
            p.status = "Active"
            p.flags.ignore_links = True
            p.insert(ignore_permissions=True)
            frappe.db.commit()
        cls.product_name = frappe.db.get_value("Product", {"product_name": cls.PRODUCT}, "name")

    @classmethod
    def tearDownClass(cls):
        # Cleanup
        for docname in [cls.vendor_name, cls.product_name]:
            if docname and frappe.db.exists("Vendor", docname):
                frappe.delete_doc("Vendor", docname, force=True)
            if docname and frappe.db.exists("Product", docname):
                frappe.delete_doc("Product", docname, force=True)
        frappe.db.commit()

    def _add_warehouses(self):
        vdoc = frappe.get_doc("Vendor", self.vendor_name)
        vdoc.warehouses = []
        vdoc.append("warehouses", {
            "warehouse_name": "Kathmandu Store",
            "lat": 27.7172, "lng": 85.3240,
            "is_default": 1, "priority": 1, "status": "Active",
        })
        vdoc.append("warehouses", {
            "warehouse_name": "Pokhara Store",
            "lat": 28.2096, "lng": 83.9856,
            "is_default": 0, "priority": 2, "status": "Active",
        })
        vdoc.append("warehouses", {
            "warehouse_name": "Chitwan Store",
            "lat": 27.5292, "lng": 84.3542,
            "is_default": 0, "priority": 3, "status": "Active",
        })
        vdoc.flags.ignore_links = True
        vdoc.save(ignore_permissions=True)
        frappe.db.commit()

    def test_create_warehouses(self):
        self._add_warehouses()
        vdoc = frappe.get_doc("Vendor", self.vendor_name)
        self.assertEqual(len(vdoc.warehouses), 3)

    def test_list_warehouses(self):
        self._add_warehouses()
        whs = get_vendor_warehouses(self.vendor_name)
        self.assertEqual(len(whs), 3)
        names = [w.warehouse_name for w in whs]
        self.assertIn("Kathmandu Store", names)

    def test_default_warehouse(self):
        self._add_warehouses()
        dw = get_default_warehouse(self.vendor_name)
        self.assertEqual(dw, "Kathmandu Store")

    def test_only_one_default(self):
        self._add_warehouses()
        vdoc = frappe.get_doc("Vendor", self.vendor_name)
        for wh in vdoc.warehouses:
            if wh.warehouse_name == "Pokhara Store":
                wh.is_default = 1
        vdoc.flags.ignore_links = True
        with self.assertRaises((frappe.ValidationError, frappe.exceptions.ValidationError)):
            vdoc.save(ignore_permissions=True)

    def test_inactive_excluded(self):
        self._add_warehouses()
        vdoc = frappe.get_doc("Vendor", self.vendor_name)
        for wh in vdoc.warehouses:
            if wh.warehouse_name == "Chitwan Store":
                wh.status = "Inactive"
        vdoc.flags.ignore_links = True
        vdoc.save(ignore_permissions=True)
        frappe.db.commit()
        whs = get_vendor_warehouses(self.vendor_name)
        self.assertEqual(len(whs), 2)
        # Restore
        vdoc = frappe.get_doc("Vendor", self.vendor_name)
        for wh in vdoc.warehouses:
            if wh.warehouse_name == "Chitwan Store":
                wh.status = "Active"
        vdoc.flags.ignore_links = True
        vdoc.save(ignore_permissions=True)
        frappe.db.commit()


class TestNearestWarehouse(unittest.TestCase):
    VENDOR = "WH Test Vendor Routing"

    @classmethod
    def setUpClass(cls):
        frappe.set_user("Administrator")
        if not frappe.db.exists("Vendor", {"vendor_name": cls.VENDOR}):
            v = frappe.new_doc("Vendor")
            v.vendor_name = cls.VENDOR
            v.slug = "wh-test-routing"
            v.status = "Active"
            v.default_warehouse = "Default Store"
            v.flags.ignore_links = True
            v.insert(ignore_permissions=True)
            frappe.db.commit()
        cls.vendor_name = frappe.db.get_value("Vendor", {"vendor_name": cls.VENDOR}, "name")
        # Add warehouses
        vdoc = frappe.get_doc("Vendor", cls.vendor_name)
        vdoc.warehouses = []
        vdoc.append("warehouses", {
            "warehouse_name": "Kathmandu Store",
            "lat": 27.7172, "lng": 85.3240,
            "is_default": 1, "priority": 1, "status": "Active",
        })
        vdoc.append("warehouses", {
            "warehouse_name": "Pokhara Store",
            "lat": 28.2096, "lng": 83.9856,
            "is_default": 0, "priority": 2, "status": "Active",
        })
        vdoc.append("warehouses", {
            "warehouse_name": "Chitwan Store",
            "lat": 27.5292, "lng": 84.3542,
            "is_default": 0, "priority": 3, "status": "Active",
        })
        vdoc.flags.ignore_links = True
        vdoc.save(ignore_permissions=True)
        frappe.db.commit()

    @classmethod
    def tearDownClass(cls):
        if frappe.db.exists("Vendor", cls.vendor_name):
            frappe.delete_doc("Vendor", cls.vendor_name, force=True)
            frappe.db.commit()

    def test_nearest_to_kathmandu(self):
        r = find_nearest_warehouse(self.vendor_name, 27.7172, 85.3240)
        self.assertEqual(r["warehouse_name"], "Kathmandu Store")

    def test_nearest_to_pokhara(self):
        r = find_nearest_warehouse(self.vendor_name, 28.2096, 83.9856)
        self.assertEqual(r["warehouse_name"], "Pokhara Store")

    def test_nearest_to_chitwan(self):
        r = find_nearest_warehouse(self.vendor_name, 27.5292, 84.3542)
        self.assertEqual(r["warehouse_name"], "Chitwan Store")

    def test_no_location_returns_default(self):
        r = find_nearest_warehouse(self.vendor_name, None, None)
        self.assertEqual(r["warehouse_name"], "Kathmandu Store")

    def test_distance_at_same_point(self):
        r = find_nearest_warehouse(self.vendor_name, 27.7172, 85.3240)
        self.assertEqual(r["distance_km"], 0.0)

    def test_distance_between_cities(self):
        # Search from Kathmandu, nearest should be Kathmandu (0km),
        # and Pokhara should be ~142km away
        r_pokhara = find_nearest_warehouse(self.vendor_name, 28.2096, 83.9856)
        self.assertEqual(r_pokhara["warehouse_name"], "Pokhara Store")
        self.assertEqual(r_pokhara["distance_km"], 0.0)
        # Verify non-zero distance exists
        from saathimart.api.warehouses import _haversine_km
        d = _haversine_km(27.7172, 85.3240, 28.2096, 83.9856)
        self.assertGreater(d, 130)


class TestPerWarehouseStock(unittest.TestCase):
    VENDOR = "WH Test Vendor Stock"
    PRODUCT = "WH Test Product Stock"

    @classmethod
    def setUpClass(cls):
        frappe.set_user("Administrator")
        if not frappe.db.exists("Vendor", {"vendor_name": cls.VENDOR}):
            v = frappe.new_doc("Vendor")
            v.vendor_name = cls.VENDOR
            v.slug = "wh-test-stock"
            v.status = "Active"
            v.default_warehouse = "Default Store"
            v.flags.ignore_links = True
            v.insert(ignore_permissions=True)
            frappe.db.commit()
        cls.vendor_name = frappe.db.get_value("Vendor", {"vendor_name": cls.VENDOR}, "name")

        if not frappe.db.exists("Product", {"product_name": cls.PRODUCT}):
            p = frappe.new_doc("Product")
            p.product_name = cls.PRODUCT
            p.slug = "wh-test-prod-stock"
            p.status = "Active"
            p.flags.ignore_links = True
            p.insert(ignore_permissions=True)
            frappe.db.commit()
        cls.product_name = frappe.db.get_value("Product", {"product_name": cls.PRODUCT}, "name")

    @classmethod
    def tearDownClass(cls):
        # Cleanup vendor stock rows
        for wh in ["default", "Kathmandu Store", "Pokhara Store", "Test WH"]:
            rname = _row_name(cls.vendor_name, cls.product_name, wh)
            if frappe.db.exists("Vendor Stock", rname):
                frappe.delete_doc("Vendor Stock", rname, force=True)
        for docname in [cls.vendor_name, cls.product_name]:
            if docname and frappe.db.exists("Vendor", docname):
                frappe.delete_doc("Vendor", docname, force=True)
            if docname and frappe.db.exists("Product", docname):
                frappe.delete_doc("Product", docname, force=True)
        frappe.db.commit()

    def test_default_warehouse_stock_row(self):
        row = get_or_create(self.vendor_name, self.product_name)
        self.assertEqual(row.warehouse, "default")
        self.assertEqual(row.is_default_warehouse, 1)

    def test_specific_warehouse_stock_row(self):
        row = get_or_create(self.vendor_name, self.product_name, "Kathmandu Store")
        self.assertEqual(row.warehouse, "Kathmandu Store")
        self.assertEqual(row.is_default_warehouse, 0)

    def test_row_name_format(self):
        name_default = _row_name(self.vendor_name, self.product_name, "default")
        self.assertEqual(name_default, f"{self.vendor_name}-{self.product_name}-default")
        name_wh = _row_name(self.vendor_name, self.product_name, "Kathmandu Store")
        self.assertEqual(name_wh, f"{self.vendor_name}-{self.product_name}-Kathmandu Store")

    def test_different_warehouses_are_separate(self):
        row1 = get_or_create(self.vendor_name, self.product_name, "Kathmandu Store")
        row2 = get_or_create(self.vendor_name, self.product_name, "Pokhara Store")
        self.assertNotEqual(row1.name, row2.name)

    def test_per_warehouse_stock_query(self):
        row_ktm = get_or_create(self.vendor_name, self.product_name, "Kathmandu Store")
        frappe.db.set_value("Vendor Stock", row_ktm.name, {"available_qty": 50})
        row_pkr = get_or_create(self.vendor_name, self.product_name, "Pokhara Store")
        frappe.db.set_value("Vendor Stock", row_pkr.name, {"available_qty": 30})
        frappe.db.commit()

        stock = get_stock_by_warehouse(self.product_name)
        self.assertIn(self.vendor_name, stock)
        self.assertEqual(stock[self.vendor_name]["Kathmandu Store"]["available_qty"], 50)
        self.assertEqual(stock[self.vendor_name]["Pokhara Store"]["available_qty"], 30)

    def test_physical_qty_sync(self):
        row = get_or_create(self.vendor_name, self.product_name, "Test WH")
        frappe.db.set_value("Vendor Stock", row.name, {"available_qty": 20, "reserved_qty": 5})
        row.reload()
        row.save()
        self.assertEqual(row.physical_qty, 25)

    def test_uniqueness_enforcement(self):
        row1 = get_or_create(self.vendor_name, self.product_name, "Test Uniq WH")
        doc2 = frappe.new_doc("Vendor Stock")
        doc2.vendor = self.vendor_name
        doc2.product = self.product_name
        doc2.warehouse = "Test Uniq WH"
        with self.assertRaises((frappe.ValidationError, frappe.DuplicateEntryError)):
            doc2.insert(ignore_permissions=True)
        # Cleanup
        if frappe.db.exists("Vendor Stock", row1.name):
            frappe.delete_doc("Vendor Stock", row1.name, force=True)
            frappe.db.commit()


class TestBackwardCompatibility(unittest.TestCase):
    VENDOR = "WH Test Vendor BackCompat"

    @classmethod
    def setUpClass(cls):
        frappe.set_user("Administrator")
        if not frappe.db.exists("Vendor", {"vendor_name": cls.VENDOR}):
            v = frappe.new_doc("Vendor")
            v.vendor_name = cls.VENDOR
            v.slug = "wh-test-backcompat"
            v.status = "Active"
            v.default_warehouse = "Single Store"
            v.flags.ignore_links = True
            v.insert(ignore_permissions=True)
            frappe.db.commit()
        cls.vendor_name = frappe.db.get_value("Vendor", {"vendor_name": cls.VENDOR}, "name")

    @classmethod
    def tearDownClass(cls):
        if frappe.db.exists("Vendor", cls.vendor_name):
            frappe.delete_doc("Vendor", cls.vendor_name, force=True)
            frappe.db.commit()

    def test_no_warehouses_falls_back_to_default(self):
        r = find_nearest_warehouse(self.vendor_name, 27.7172, 85.3240)
        self.assertEqual(r["warehouse_name"], "Single Store")

    def test_get_warehouses_empty(self):
        whs = get_vendor_warehouses(self.vendor_name)
        self.assertEqual(len(whs), 0)


if __name__ == "__main__":
    unittest.main()
