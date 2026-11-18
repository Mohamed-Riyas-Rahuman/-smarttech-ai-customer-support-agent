"""Idempotent database seeding script for SmartTech Support Agent."""
from datetime import datetime, timedelta, timezone
from sqlalchemy import select
from app.core.security import hash_password
from app.core.config import settings
from app.db.session import SessionLocal
from app.models.user import User
from app.models.customer import Customer
from app.models.product import Product
from app.models.order import Order, OrderItem
from app.models.device import Device
from app.models.warranty import Warranty
from app.models.ticket import SupportTicket, TicketMessage
from app.models.email import EmailMessage
from app.models.draft import Draft
from app.models.audit import AuditLog, SystemSetting
from app.services.draft_policy import body_hash


def seed_database():
    """Populates realistic demo dataset without duplicating existing records."""
    db = SessionLocal()
    now = datetime.now(timezone.utc)
    print("Beginning idempotent database seed...")

    try:
        # 1. System Users
        users_data = [
            ("admin@smarttech.local", "Alex Mercer (Admin)", settings.seed_admin_password, "admin", True),
            ("manager@smarttech.local", "Rachel Zhang (Manager)", settings.seed_manager_password, "manager", settings.seed_demo_users),
            ("agent@smarttech.local", "Liam O'Connor (Agent)", settings.seed_agent_password, "agent", settings.seed_demo_users),
            ("analyst@smarttech.local", "Maya Patel (Analyst)", settings.seed_analyst_password, "analyst", settings.seed_demo_users),
        ]
        # Validate every required credential before writing any seed data.
        existing_users = {
            email: db.scalar(select(User).where(User.email == email))
            for email, *_ in users_data
        }
        for email, _name, raw_pwd, role, enabled in users_data:
            if not enabled or existing_users[email]:
                continue
            env_name = f"SEED_{role.upper()}_PASSWORD"
            if not raw_pwd or len(raw_pwd) < 12:
                raise RuntimeError(f"{env_name} is required (minimum 12 characters) to create {role} seed account {email}.")
        user_map = {}
        for email, full_name, raw_pwd, role, enabled in users_data:
            user = existing_users[email]
            if not user:
                if not enabled:
                    continue
                user = User(
                    email=email,
                    full_name=full_name,
                    hashed_password=hash_password(raw_pwd),
                    role=role,
                    is_active=True,
                )
                db.add(user)
                db.flush()
                print(f"Created user: {email} ({role})")
            user_map[role] = user

        # 2. Products
        products_data = [
            ("ST-AUD-001", "SoundPulse Pro ANC Headphones", "Audio", "Flagship active noise-cancelling wireless headphones with 40-hour battery life and spatial audio.", 249.99, 24, "active", 2025),
            ("ST-VIS-002", "VisionBeam 4K Portable Projector", "Display", "Ultra-compact true 4K laser projector with 2000 ANSI lumens, HDR10+, and Android TV 12.", 699.00, 12, "active", 2024),
            ("ST-CMP-003", "AeroBook Ultra 14 Laptop", "Computing", "High-performance thin-and-light laptop featuring Intel Core Ultra 7, 32GB LPDDR5X, and 120Hz OLED screen.", 1199.00, 12, "active", 2025),
            ("ST-IOT-004", "OmniHub Smart Home Gateway", "SmartHome", "Matter and Zigbee 3.0 compatible central smart home automation hub with local offline execution.", 179.99, 12, "active", 2024),
            ("ST-PWR-005", "PowerWave 100W GaN Charging Station", "Accessories", "Compact 4-port fast GaN charger with dual USB-C Power Delivery 3.0 and intelligent power allocation.", 79.99, 18, "active", 2025),
        ]
        product_map = {}
        for sku, name, cat, desc, price, w_months, status, r_year in products_data:
            prod = db.scalar(select(Product).where(Product.sku == sku))
            if not prod:
                prod = Product(
                    sku=sku,
                    name=name,
                    category=cat,
                    description=desc,
                    price=price,
                    warranty_months=w_months,
                    support_status=status,
                    release_year=r_year,
                )
                db.add(prod)
                db.flush()
                print(f"Created product: {sku} - {name}")
            product_map[sku] = prod

        # 3. Customers
        customers_data = [
            ("marcus.vance@example.com", "Marcus Vance", "+1-555-0192", "Apex Dynamics", "104 Innovation Way, Austin, TX 78701"),
            ("elena.rostova@example.com", "Elena Rostova", "+1-555-0144", "TechNova Systems", "420 Silicon Blvd, San Jose, CA 95110"),
            ("david.chen@example.com", "David Chen", "+1-555-0188", None, "77 Pine Street, Seattle, WA 98101"),
            ("sarah.jenkins@example.com", "Sarah Jenkins", "+1-555-0112", None, "512 Maple Avenue, Denver, CO 80202"),
            ("emily.watson@example.com", "Emily Watson", "+1-555-0176", "Watson Consulting", "88 Market St, Boston, MA 02110"),
        ]
        customer_map = {}
        for email, name, phone, company, addr in customers_data:
            cust = db.scalar(select(Customer).where(Customer.email == email))
            if not cust:
                cust = Customer(
                    email=email,
                    full_name=name,
                    phone=phone,
                    company=company,
                    address=addr,
                    notes="Verified customer with active hardware registration.",
                )
                db.add(cust)
                db.flush()
                print(f"Created customer: {name} ({email})")
            customer_map[email] = cust

        # 4. Orders & Order Items
        orders_data = [
            ("ORD-2026-1049", "marcus.vance@example.com", "delivered", 249.99, "FedEx", "FX-84920491", now - timedelta(days=45), [("ST-AUD-001", 1, 249.99, "ST-SN-AUD-88219")]),
            ("ORD-2026-1082", "elena.rostova@example.com", "delivered", 1199.00, "UPS", "1Z9999999999999999", now - timedelta(days=30), [("ST-CMP-003", 1, 1199.00, "ST-SN-CMP-10492")]),
            ("ORD-2026-1120", "david.chen@example.com", "delivered", 699.00, "FedEx", "FX-11029482", now - timedelta(days=420), [("ST-VIS-002", 1, 699.00, "ST-SN-VIS-33910")]),
            ("ORD-2026-1155", "sarah.jenkins@example.com", "shipped", 179.99, "USPS", "940011189956281928", now - timedelta(days=4), [("ST-IOT-004", 1, 179.99, "ST-SN-IOT-77124")]),
        ]
        order_map = {}
        for ord_num, c_email, status, total, carrier, track, ord_date, items in orders_data:
            order = db.scalar(select(Order).where(Order.order_number == ord_num))
            if not order:
                order = Order(
                    order_number=ord_num,
                    customer_id=customer_map[c_email].id,
                    status=status,
                    total_amount=total,
                    carrier=carrier,
                    tracking_number=track,
                    order_date=ord_date,
                    shipping_address=customer_map[c_email].address,
                )
                db.add(order)
                db.flush()
                for sku, qty, price, sn in items:
                    item = OrderItem(
                        order_id=order.id,
                        product_id=product_map[sku].id,
                        quantity=qty,
                        unit_price=price,
                        serial_number=sn,
                    )
                    db.add(item)
                db.flush()
                print(f"Created order: {ord_num} for {c_email}")
            order_map[ord_num] = order

        # 5. Devices & Warranties
        devices_data = [
            ("ST-SN-AUD-88219", "marcus.vance@example.com", "ST-AUD-001", "ORD-2026-1049", "Marcus's Headphones", now - timedelta(days=40), "active", now - timedelta(days=40), now + timedelta(days=690)),
            ("ST-SN-CMP-10492", "elena.rostova@example.com", "ST-CMP-003", "ORD-2026-1082", "Work AeroBook", now - timedelta(days=25), "active", now - timedelta(days=25), now + timedelta(days=340)),
            ("ST-SN-VIS-33910", "david.chen@example.com", "ST-VIS-002", "ORD-2026-1120", "Living Room Projector", now - timedelta(days=415), "expired", now - timedelta(days=415), now - timedelta(days=50)),
            ("ST-SN-IOT-77124", "sarah.jenkins@example.com", "ST-IOT-004", "ORD-2026-1155", "Home Gateway", now - timedelta(days=2), "active", now - timedelta(days=2), now + timedelta(days=363)),
        ]
        device_map = {}
        for sn, c_email, sku, ord_num, nickname, p_date, w_status, w_start, w_end in devices_data:
            device = db.scalar(select(Device).where(Device.serial_number == sn))
            if not device:
                device = Device(
                    serial_number=sn,
                    customer_id=customer_map[c_email].id,
                    product_id=product_map[sku].id,
                    order_id=order_map[ord_num].id,
                    nickname=nickname,
                    purchase_date=p_date,
                    is_registered=True,
                )
                db.add(device)
                db.flush()
            warranty = db.scalar(select(Warranty).where(Warranty.device_id == device.id))
            if not warranty:
                warranty = Warranty(
                    policy_number=f"WAR-{sn[-8:]}",
                    device_id=device.id,
                    customer_id=customer_map[c_email].id,
                    coverage_type="standard",
                    status=w_status,
                    start_date=w_start,
                    end_date=w_end,
                    terms_summary=f"Covers hardware defects and manufacturer faults for {product_map[sku].name}.",
                )
                db.add(warranty)
                db.flush()
                print(f"Created registered device & warranty: {sn} ({w_status})")
            device_map[sn] = device

        # 6. Support Tickets, Messages, Emails & Drafts
        tickets_data = [
            {
                "number": "TCK-2026-001",
                "customer": "marcus.vance@example.com",
                "order": "ORD-2026-1049",
                "device": "ST-SN-AUD-88219",
                "assigned": "agent",
                "subject": "Firmware update failing on SoundPulse Pro",
                "status": "awaiting_approval",
                "priority": "high",
                "category": "technical",
                "sentiment": "frustrated",
                "escalation": "none",
                "created_at": now - timedelta(hours=3),
                "messages": [
                    ("customer", "marcus.vance@example.com", "Marcus Vance", "Hi support team,\n\nI purchased the SoundPulse Pro headphones (Order #ORD-2026-1049, SN ST-SN-AUD-88219). Every time I attempt to update to firmware 2.4.1 through the mobile app, the update stalls at 45% and the LED blinks red rapidly. I tried restarting my phone and charging the headphones to 100%, but it continues failing. Can you provide troubleshooting steps?\n\nThanks,\nMarcus Vance", False),
                ],
                "email_provider_id": "gmail_msg_001_vance",
                "draft_body": (
                    "Dear Marcus,\n\n"
                    "Thank you for contacting SmartTech Support regarding your SoundPulse Pro ANC Headphones (Serial Number: ST-SN-AUD-88219, Order #ORD-2026-1049).\n\n"
                    "According to our official SoundPulse Pro Hardware Troubleshooting Manual (Section 4.2), a rapid red LED during firmware updates indicates a Bluetooth packet timeout during flash staging. Please perform the following hard reset procedure:\n\n"
                    "1. Disconnect the headphones from any charging cable.\n"
                    "2. Hold down the Power button and Noise Cancellation (ANC) button simultaneously for 12 seconds until the LED flashes purple twice.\n"
                    "3. In your smartphone's Bluetooth settings, choose 'Forget This Device'.\n"
                    "4. Power on the headphones, re-pair via Bluetooth, and retry the update in the SmartTech Companion App.\n\n"
                    "Your device is covered under our 24-month manufacturer warranty (Policy #WAR-UD-88219, valid through February 2028). If the reset does not resolve the issue, we will happily arrange a replacement.\n\n"
                    "Best regards,\n"
                    "SmartTech Technical Support Team"
                ),
                "draft_status": "pending_review",
            },
            {
                "number": "TCK-2026-002",
                "customer": "elena.rostova@example.com",
                "order": "ORD-2026-1082",
                "device": "ST-SN-CMP-10492",
                "assigned": "agent",
                "subject": "HDMI flickering when connecting 4K monitor",
                "status": "in_progress",
                "priority": "medium",
                "category": "technical",
                "sentiment": "neutral",
                "escalation": "none",
                "created_at": now - timedelta(hours=8),
                "messages": [
                    ("customer", "elena.rostova@example.com", "Elena Rostova", "Hello, I am using the AeroBook Ultra 14 with an external Dell 4K display via USB-C to HDMI adapter. At 60Hz the signal drops out for 2 seconds every few minutes. Is there a thunderbolt display driver update?", False),
                    ("agent", "agent@smarttech.local", "Liam O'Connor", "Hi Elena, thanks for reaching out. We are verifying the Intel Iris Xe Thunderbolt driver package version compatible with your OS build.", False),
                ],
                "email_provider_id": "gmail_msg_002_rostova",
                "draft_body": None,
                "draft_status": None,
            },
            {
                "number": "TCK-2026-003",
                "customer": "david.chen@example.com",
                "order": "ORD-2026-1120",
                "device": "ST-SN-VIS-33910",
                "assigned": "manager",
                "subject": "Lens focus motor grinding noise on VisionBeam 4K",
                "status": "open",
                "priority": "low",
                "category": "warranty",
                "sentiment": "negative",
                "escalation": "recommended",
                "created_at": now - timedelta(hours=14),
                "messages": [
                    ("customer", "david.chen@example.com", "David Chen", "My VisionBeam 4K projector makes a loud grinding noise during autofocus and cannot sharpen the image. I would like a free warranty repair or replacement.", False),
                ],
                "email_provider_id": "gmail_msg_003_chen",
                "draft_body": None,
                "draft_status": None,
            },
            {
                "number": "TCK-2026-004",
                "customer": "sarah.jenkins@example.com",
                "order": "ORD-2026-1155",
                "device": "ST-SN-IOT-77124",
                "assigned": "manager",
                "subject": "Urgent: Package marked delivered but NOT received",
                "status": "open",
                "priority": "urgent",
                "category": "order",
                "sentiment": "frustrated",
                "escalation": "escalated",
                "created_at": now - timedelta(hours=1),
                "messages": [
                    ("customer", "sarah.jenkins@example.com", "Sarah Jenkins", "URGENT: Order #ORD-2026-1155 was supposedly delivered yesterday via USPS tracking 940011189956281928. I have checked with my neighbors and building concierge and no package arrived. This was for an urgent smart home installation. Please assist immediately!", False),
                ],
                "email_provider_id": "gmail_msg_004_jenkins",
                "draft_body": None,
                "draft_status": None,
            },
            {
                "number": "TCK-2026-005",
                "customer": "emily.watson@example.com",
                "order": None,
                "device": None,
                "assigned": "agent",
                "subject": "OmniHub Zigbee 3.0 protocol inquiry",
                "status": "resolved",
                "priority": "low",
                "category": "general",
                "sentiment": "positive",
                "escalation": "none",
                "created_at": now - timedelta(days=2),
                "messages": [
                    ("customer", "emily.watson@example.com", "Emily Watson", "Does the OmniHub support Zigbee Green Power devices out of the box?", False),
                    ("agent", "agent@smarttech.local", "Liam O'Connor", "Hi Emily, yes, firmware v1.8 and above supports Zigbee Green Power (ZGP) proxy and sink devices directly.", False),
                ],
                "email_provider_id": "gmail_msg_005_watson",
                "draft_body": "Hi Emily,\n\nYes, the OmniHub Smart Home Gateway fully supports Zigbee Green Power (ZGP) devices as of firmware version 1.8.\n\nBest regards,\nSmartTech Support",
                "draft_status": "sent",
            },
        ]

        for t_info in tickets_data:
            ticket = db.scalar(select(SupportTicket).where(SupportTicket.ticket_number == t_info["number"]))
            if not ticket:
                cust = customer_map[t_info["customer"]]
                ord_id = order_map[t_info["order"]].id if t_info["order"] else None
                dev_id = device_map[t_info["device"]].id if t_info["device"] else None
                assigned_user = user_map.get(t_info["assigned"]) if t_info["assigned"] else None
                assigned_id = assigned_user.id if assigned_user else None

                ticket = SupportTicket(
                    ticket_number=t_info["number"],
                    customer_id=cust.id,
                    order_id=ord_id,
                    device_id=dev_id,
                    assigned_to_user_id=assigned_id,
                    subject=t_info["subject"],
                    status=t_info["status"],
                    priority=t_info["priority"],
                    category=t_info["category"],
                    sentiment=t_info["sentiment"],
                    escalation_status=t_info["escalation"],
                    created_at=t_info["created_at"],
                )
                db.add(ticket)
                db.flush()

                # Add messages
                for s_type, s_email, s_name, msg_body, is_int in t_info["messages"]:
                    msg = TicketMessage(
                        ticket_id=ticket.id,
                        sender_type=s_type,
                        sender_email=s_email,
                        sender_name=s_name,
                        message_body=msg_body,
                        is_internal_note=is_int,
                        created_at=t_info["created_at"],
                    )
                    db.add(msg)

                # Add EmailMessage
                email_msg = EmailMessage(
                    provider_message_id=t_info["email_provider_id"],
                    thread_id=f"thread_{t_info['number'].lower()}",
                    ticket_id=ticket.id,
                    sender=t_info["customer"],
                    recipient="support@smarttech.local",
                    subject=t_info["subject"],
                    body_text=t_info["messages"][0][3],
                    is_processed=True,
                    received_at=t_info["created_at"],
                )
                db.add(email_msg)
                db.flush()

                # Add Draft if present
                if t_info["draft_body"]:
                    b = t_info["draft_body"]
                    status = t_info["draft_status"]
                    approver = user_map.get("manager") or user_map["admin"]
                    appr_by = approver.id if status in ("approved", "sent") else None
                    appr_hash = body_hash(b) if appr_by else None
                    sent_time = now - timedelta(days=1) if status == "sent" else None

                    draft = Draft(
                        ticket_id=ticket.id,
                        email_id=email_msg.id,
                        recipient=t_info["customer"],
                        subject=f"Re: {t_info['subject']}",
                        body=b,
                        status=status,
                        approved_by=appr_by,
                        approved_body_hash=appr_hash,
                        sent_at=sent_time,
                        citations='[{"document": "SoundPulse_Pro_Manual.pdf", "section": "4.2 Factory Reset"}]' if "SoundPulse" in b else None,
                    )
                    db.add(draft)

                print(f"Created ticket: {t_info['number']} - {t_info['subject']}")

        # 7. System Settings
        settings_data = [
            ("auto_send_enabled", "false", "Enforces human review before email dispatch. Strict default.", False),
            ("ai_confidence_threshold", "0.70", "Minimum LLM classification confidence required to draft without escalation warning.", False),
            ("support_inbox_email", "support@smarttech.local", "Primary customer support recipient address.", False),
            ("escalation_notify_email", "support-lead@smarttech.local", "Email receiving urgent escalation notices.", False),
        ]
        for key, val, desc, is_sec in settings_data:
            s = db.scalar(select(SystemSetting).where(SystemSetting.key == key))
            if not s:
                s = SystemSetting(
                    key=key,
                    value=val,
                    description=desc,
                    is_secret=is_sec,
                    updated_by_user_id=user_map["admin"].id,
                )
                db.add(s)

        # 8. Audit logs (one marker per database, even across repeated runs).
        if not db.scalar(select(AuditLog.id).where(AuditLog.action == "database_seeded").limit(1)):
            db.add(AuditLog(
                user_id=user_map["admin"].id,
                action="database_seeded",
                entity_type="system",
                details="Database populated with safe realistic demo dataset.",
            ))

        db.commit()
        print("Database seed completed successfully!")

    except Exception as e:
        db.rollback()
        print(f"Database seed failed ({type(e).__name__}); details omitted to protect configuration secrets.")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    seed_database()
