"""
Controlled synthetic dataset fixture for privacy, identity, and AI workflow testing.
Contains 12 clearly identified synthetic test customers with realistic associated records:
- Profiles, orders, order items, shipments, tracking
- Devices, serial numbers, warranty records
- Clear data isolation across customers
"""

from datetime import datetime, date, timedelta
from pathlib import Path
from typing import Dict, Any, List
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.customer import Customer
from app.models.product import Product
from app.models.order import Order, OrderItem
from app.models.device import Device
from app.models.warranty import Warranty
from app.models.ticket import SupportTicket, TicketMessage
from app.models.draft import Draft
from app.models.knowledge import KnowledgeDocument
from app.services.rag.retrieval import ingest_document


SYNTHETIC_CUSTOMERS_DATA = [
    {
        "full_name": "Alice Anderson",
        "email": "alice.anderson@example.org",
        "phone": "+1-555-0101",
        "company": "Anderson Design",
        "order": {
            "order_number": "ORD-SYNTH-1001",
            "order_date": datetime(2026, 2, 10, 10, 30),
            "status": "shipped",
            "total_amount": 299.99,
            "shipping_address": "100 Maple St, Seattle, WA 98101",
            "tracking_number": "TRK-AA-998811",
            "carrier": "FedEx",
            "sku": "ST-AUD-001",
            "quantity": 1,
            "unit_price": 299.99,
        },
        "device": {
            "sku": "ST-AUD-001",
            "serial_number": "SN-SP-ALICE-101",
            "nickname": "Alice SoundPulse Studio",
            "purchase_date": date(2026, 2, 10),
        },
        "warranty": {
            "policy_number": "POL-WR-ALICE-01",
            "start_date": date(2026, 2, 10),
            "end_date": date(2028, 2, 10),
            "warranty_type": "extended",
            "status": "active",
        },
    },
    {
        "full_name": "Bob Baker",
        "email": "bob.baker@example.org",
        "phone": "+1-555-0102",
        "company": "Baker Media Lab",
        "order": {
            "order_number": "ORD-SYNTH-1002",
            "order_date": datetime(2026, 1, 15, 14, 0),
            "status": "delivered",
            "total_amount": 799.99,
            "shipping_address": "200 Pine Ave, Portland, OR 97201",
            "tracking_number": "TRK-BB-223344",
            "carrier": "UPS",
            "sku": "ST-VIS-002",
            "quantity": 1,
            "unit_price": 799.99,
        },
        "device": {
            "sku": "ST-VIS-002",
            "serial_number": "SN-VB-BOB-202",
            "nickname": "Bob Cinema Projector",
            "purchase_date": date(2026, 1, 15),
        },
        "warranty": {
            "policy_number": "POL-WR-BOB-02",
            "start_date": date(2026, 1, 15),
            "end_date": date(2027, 1, 15),
            "warranty_type": "standard",
            "status": "active",
        },
    },
    {
        "full_name": "Charlie Clark",
        "email": "charlie.clark@example.org",
        "phone": "+1-555-0103",
        "company": "Clark Solutions",
        "order": {
            "order_number": "ORD-SYNTH-1003",
            "order_date": datetime(2026, 3, 1, 9, 15),
            "status": "processing",
            "total_amount": 1299.00,
            "shipping_address": "300 Oak Blvd, Austin, TX 78701",
            "tracking_number": "TRK-CC-334455",
            "carrier": "USPS",
            "sku": "ST-CMP-003",
            "quantity": 1,
            "unit_price": 1299.00,
        },
        "device": {
            "sku": "ST-CMP-003",
            "serial_number": "SN-AB-CHARLIE-303",
            "nickname": "Charlie AeroBook Work",
            "purchase_date": date(2026, 3, 1),
        },
        "warranty": {
            "policy_number": "POL-WR-CHARLIE-03",
            "start_date": date(2026, 3, 1),
            "end_date": date(2027, 3, 1),
            "warranty_type": "standard",
            "status": "active",
        },
    },
    {
        "full_name": "Diana Davis",
        "email": "diana.davis@example.org",
        "phone": "+1-555-0104",
        "company": "Davis Analytics",
        "order": {
            "order_number": "ORD-SYNTH-1004",
            "order_date": datetime(2024, 4, 10, 11, 0),
            "status": "delivered",
            "total_amount": 89.99,
            "shipping_address": "400 Elm St, Denver, CO 80201",
            "tracking_number": "TRK-DD-445566",
            "carrier": "FedEx",
            "sku": "ST-PWR-005",
            "quantity": 1,
            "unit_price": 89.99,
        },
        "device": {
            "sku": "ST-PWR-005",
            "serial_number": "SN-PW-DIANA-404",
            "nickname": "Diana Desk Charger",
            "purchase_date": date(2024, 4, 10),
        },
        "warranty": {
            "policy_number": "POL-WR-DIANA-04",
            "start_date": date(2024, 4, 10),
            "end_date": date(2025, 4, 10),
            "warranty_type": "standard",
            "status": "expired",
        },
    },
    {
        "full_name": "Evan Evans",
        "email": "evan.evans@example.org",
        "phone": "+1-555-0105",
        "company": "Evans Smart Home",
        "order": {
            "order_number": "ORD-SYNTH-1005",
            "order_date": datetime(2026, 2, 20, 16, 45),
            "status": "delivered",
            "total_amount": 149.99,
            "shipping_address": "500 Cedar Way, Chicago, IL 60601",
            "tracking_number": "TRK-EE-556677",
            "carrier": "UPS",
            "sku": "ST-IOT-004",
            "quantity": 1,
            "unit_price": 149.99,
        },
        "device": {
            "sku": "ST-IOT-004",
            "serial_number": "SN-OH-EVAN-505",
            "nickname": "Evan Living Room Hub",
            "purchase_date": date(2026, 2, 20),
        },
        "warranty": {
            "policy_number": "POL-WR-EVAN-05",
            "start_date": date(2026, 2, 20),
            "end_date": date(2027, 2, 20),
            "warranty_type": "standard",
            "status": "active",
        },
    },
    {
        "full_name": "Fiona Fisher",
        "email": "fiona.fisher@example.org",
        "phone": "+1-555-0106",
        "company": "Fisher Consulting",
        # Fiona has NO orders, NO registered devices, NO warranties.
        # Intentionally empty to test unverified / zero records handling.
        "order": None,
        "device": None,
        "warranty": None,
    },
    {
        "full_name": "George Green",
        "email": "george.green@example.org",
        "phone": "+1-555-0107",
        "company": "Green Studios",
        "order": {
            "order_number": "ORD-SYNTH-1007",
            "order_date": datetime(2026, 1, 28, 12, 10),
            "status": "delivered",
            "total_amount": 1099.98,
            "shipping_address": "700 Walnut St, Boston, MA 02101",
            "tracking_number": "TRK-GG-778899",
            "carrier": "FedEx",
            "sku": "ST-AUD-001",
            "quantity": 1,
            "unit_price": 299.99,
        },
        "device": {
            "sku": "ST-AUD-001",
            "serial_number": "SN-SP-GEO-701",
            "nickname": "George Studio Headphones",
            "purchase_date": date(2026, 1, 28),
        },
        "warranty": {
            "policy_number": "POL-WR-GEO-01",
            "start_date": date(2026, 1, 28),
            "end_date": date(2027, 1, 28),
            "warranty_type": "standard",
            "status": "active",
        },
    },
    {
        "full_name": "Hannah Hill",
        "email": "hannah.hill@example.org",
        "phone": "+1-555-0108",
        "company": "Hill Media",
        "order": {
            "order_number": "ORD-SYNTH-1008",
            "order_date": datetime(2026, 3, 5, 15, 20),
            "status": "in_transit",
            "total_amount": 299.99,
            "shipping_address": "800 Birch Lane, Atlanta, GA 30301",
            "tracking_number": "TRK-HH-889900",
            "carrier": "FedEx",
            "sku": "ST-AUD-001",
            "quantity": 1,
            "unit_price": 299.99,
        },
        "device": {
            "sku": "ST-AUD-001",
            "serial_number": "SN-SP-HANNAH-808",
            "nickname": "Hannah Audio Pro",
            "purchase_date": date(2026, 3, 5),
        },
        "warranty": {
            "policy_number": "POL-WR-HANNAH-08",
            "start_date": date(2026, 3, 5),
            "end_date": date(2028, 3, 5),
            "warranty_type": "extended",
            "status": "active",
        },
    },
    {
        "full_name": "Ian Ingram",
        "email": "ian.ingram@example.org",
        "phone": "+1-555-0109",
        "company": "Ingram Tech",
        "order": None,
        "device": {
            "sku": "ST-IOT-004",
            "serial_number": "SN-OH-IAN-909",
            "nickname": "Ian IoT Hub",
            "purchase_date": date(2025, 11, 1),
        },
        "warranty": {
            "policy_number": "POL-WR-IAN-09",
            "start_date": date(2025, 11, 1),
            "end_date": date(2026, 11, 1),
            "warranty_type": "standard",
            "status": "active",
        },
    },
    {
        "full_name": "Julia Jones",
        "email": "julia.jones@example.org",
        "phone": "+1-555-0110",
        "company": "Jones Creative",
        "order": {
            "order_number": "ORD-SYNTH-1010",
            "order_date": datetime(2026, 2, 1, 8, 45),
            "status": "cancelled",
            "total_amount": 799.99,
            "shipping_address": "1010 Spruce St, Miami, FL 33101",
            "tracking_number": "TRK-JJ-101010",
            "carrier": "UPS",
            "sku": "ST-VIS-002",
            "quantity": 1,
            "unit_price": 799.99,
        },
        "device": {
            "sku": "ST-VIS-002",
            "serial_number": "SN-VB-JULIA-1010",
            "nickname": "Julia Outdoor Projector",
            "purchase_date": date(2026, 2, 1),
        },
        "warranty": {
            "policy_number": "POL-WR-JULIA-10",
            "start_date": date(2026, 2, 1),
            "end_date": date(2027, 2, 1),
            "warranty_type": "standard",
            "status": "active",
        },
    },
    {
        "full_name": "Kevin King",
        "email": "kevin.king@example.org",
        "phone": "+1-555-0111",
        "company": "King Logistics",
        "order": {
            "order_number": "ORD-SYNTH-1011",
            "order_date": datetime(2026, 2, 25, 17, 30),
            "status": "delivered",
            "total_amount": 89.99,
            "shipping_address": "1111 Cypress Ct, Phoenix, AZ 85001",
            "tracking_number": "TRK-KK-111122",
            "carrier": "FedEx",
            "sku": "ST-PWR-005",
            "quantity": 1,
            "unit_price": 89.99,
        },
        "device": {
            "sku": "ST-PWR-005",
            "serial_number": "SN-PW-KEVIN-1111",
            "nickname": "Kevin Office Charger",
            "purchase_date": date(2026, 2, 25),
        },
        "warranty": {
            "policy_number": "POL-WR-KEVIN-11",
            "start_date": date(2026, 2, 25),
            "end_date": date(2027, 2, 25),
            "warranty_type": "standard",
            "status": "active",
        },
    },
    {
        "full_name": "Laura Lopez",
        "email": "laura.lopez@example.org",
        "phone": "+1-555-0112",
        "company": "Lopez Audio",
        "order": {
            "order_number": "ORD-SYNTH-1012",
            "order_date": datetime(2026, 3, 2, 13, 0),
            "status": "shipped",
            "total_amount": 299.99,
            "shipping_address": "1212 Magnolia Rd, San Diego, CA 92101",
            "tracking_number": "TRK-LL-121233",
            "carrier": "UPS",
            "sku": "ST-AUD-001",
            "quantity": 1,
            "unit_price": 299.99,
        },
        "device": {
            "sku": "ST-AUD-001",
            "serial_number": "SN-SP-LAURA-1212",
            "nickname": "Laura Travel Headphones",
            "purchase_date": date(2026, 3, 2),
        },
        "warranty": {
            "policy_number": "POL-WR-LAURA-12",
            "start_date": date(2026, 3, 2),
            "end_date": date(2028, 3, 2),
            "warranty_type": "extended",
            "status": "active",
        },
    },
]


def seed_synthetic_dataset(db: Session) -> Dict[str, Customer]:
    """
    Seeds 12 synthetic customers and their isolated orders, devices, and warranties.
    Idempotent: if a customer with the synthetic email exists, reuses it without duplicating.
    Never modifies existing protected production tickets.
    """
    products_by_sku = {p.sku: p for p in db.query(Product).all()}
    seeded_customers: Dict[str, Customer] = {}

    for cdata in SYNTHETIC_CUSTOMERS_DATA:
        cust = db.scalar(select(Customer).where(Customer.email == cdata["email"]))
        if not cust:
            cust = Customer(
                full_name=cdata["full_name"],
                email=cdata["email"],
                phone=cdata["phone"],
                company=cdata["company"],
            )
            db.add(cust)
            db.flush()

        seeded_customers[cust.email] = cust

        # 1. Order
        odata = cdata.get("order")
        if odata:
            existing_ord = db.scalar(
                select(Order).where(Order.order_number == odata["order_number"])
            )
            if not existing_ord:
                order = Order(
                    customer_id=cust.id,
                    order_number=odata["order_number"],
                    order_date=odata["order_date"],
                    status=odata["status"],
                    total_amount=odata["total_amount"],
                    shipping_address=odata["shipping_address"],
                    tracking_number=odata.get("tracking_number"),
                    carrier=odata.get("carrier"),
                )
                db.add(order)
                db.flush()

                prod = products_by_sku.get(odata["sku"])
                if prod:
                    item = OrderItem(
                        order_id=order.id,
                        product_id=prod.id,
                        quantity=odata["quantity"],
                        unit_price=odata["unit_price"],
                    )
                    db.add(item)
                    db.flush()

        # 2. Device
        ddata = cdata.get("device")
        dev = None
        if ddata:
            existing_dev = db.scalar(
                select(Device).where(Device.serial_number == ddata["serial_number"])
            )
            if not existing_dev:
                prod = products_by_sku.get(ddata["sku"])
                if prod:
                    dev = Device(
                        customer_id=cust.id,
                        product_id=prod.id,
                        serial_number=ddata["serial_number"],
                        nickname=ddata["nickname"],
                        purchase_date=ddata["purchase_date"],
                    )
                    db.add(dev)
                    db.flush()
            else:
                dev = existing_dev

        # 3. Warranty
        wdata = cdata.get("warranty")
        if wdata and dev:
            existing_w = db.scalar(
                select(Warranty).where(
                    Warranty.policy_number == wdata["policy_number"]
                )
            )
            if not existing_w:
                s_dt = datetime.combine(wdata["start_date"], datetime.min.time()) if isinstance(wdata["start_date"], date) else wdata["start_date"]
                e_dt = datetime.combine(wdata["end_date"], datetime.min.time()) if isinstance(wdata["end_date"], date) else wdata["end_date"]
                warranty = Warranty(
                    device_id=dev.id,
                    customer_id=cust.id,
                    policy_number=wdata["policy_number"],
                    start_date=s_dt,
                    end_date=e_dt,
                    coverage_type=wdata.get("warranty_type", "standard"),
                    status=wdata["status"],
                    terms_summary="Standard manufacturer warranty coverage",
                )
                db.add(warranty)
                db.flush()

    db.commit()
    # Privacy/workflow tests that ask for grounded troubleshooting must not
    # depend on a prior test having indexed the sample manuals.
    sample_dir = Path(__file__).resolve().parents[3] / "data" / "sample_knowledge_base"
    sample_docs = [
        ("SoundPulse_Pro_Manual.txt", "SoundPulse Pro ANC Headphones User & Troubleshooting Guide", "manual", "ST-AUD-001"),
        ("VisionBeam_4K_Projector_Guide.md", "VisionBeam 4K Portable Projector User & Calibration Manual", "manual", "ST-VIS-002"),
        ("AeroBook_Ultra_14_Manual.txt", "AeroBook Ultra 14 Display & Hardware Manual", "manual", "ST-CMP-003"),
        ("SmartTech_Warranty_Policy.md", "SmartTech Global Warranty, Refund, and Return Policies", "policy", None),
    ]
    for filename, title, doc_type, sku in sample_docs:
        file_path = sample_dir / filename
        if not file_path.is_file():
            continue
        product_id = next((product.id for product in products_by_sku.values() if product.sku == sku), None) if sku else None
        document = db.scalar(select(KnowledgeDocument).where(KnowledgeDocument.filename == filename))
        if not document:
            document = KnowledgeDocument(
                title=title,
                filename=filename,
                file_path=str(file_path),
                file_type=file_path.suffix.lstrip("."),
                file_size_bytes=file_path.stat().st_size,
                document_type=doc_type,
                product_id=product_id,
                is_indexed=False,
            )
            db.add(document)
            db.commit()
            db.refresh(document)
        if not document.is_indexed:
            ingest_document(document.id, db)
    return seeded_customers
