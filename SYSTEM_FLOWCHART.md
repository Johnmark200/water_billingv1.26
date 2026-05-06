# Water Billing System Flowchart

This flowchart is based on the current Django codebase in `billing/`, `templates/`, and `waterbilling_project/`.

```mermaid
flowchart TD
    User["User Browser"] --> Router["Django URL Router<br/>waterbilling_project/urls.py"]
    Router --> Views["billing.views"]

    Views --> Auth["Home / Signup / Login / Logout"]
    Auth --> Dashboard["dashboard()"]
    Dashboard --> RoleCheck{"Resolve role from<br/>ConsumerProfile"}

    RoleCheck --> Admin["Admin Panel"]
    RoleCheck --> Secretary["Secretary Panel"]
    RoleCheck --> Treasurer["Treasurer Panel"]
    RoleCheck --> Reader["Reader Panel"]
    RoleCheck --> Consumer["Consumer Panel"]

    subgraph CoreData["Core Data Layer"]
        UserModel["auth_user"]
        Profile["ConsumerProfile"]
        ConsumerRecord["Consumer"]
        Reading["MeterReading"]
        Billing["BillingRecord"]
        Payment["Payment"]
        Settings["SystemSettings"]
        Notice["Notification"]
        Blast["SMSBlast"]
        Audit["AuditLog"]
        DB[("SQLite / MySQL Database")]

        UserModel --> Profile
        Profile --> ConsumerRecord
        ConsumerRecord --> Reading
        ConsumerRecord --> Billing
        ConsumerRecord --> Payment
        Billing --> Payment
        ConsumerRecord --> Notice
        Reading --> Notice
        Billing --> Notice
        Payment --> Notice

        UserModel --> DB
        Profile --> DB
        ConsumerRecord --> DB
        Reading --> DB
        Billing --> DB
        Payment --> DB
        Settings --> DB
        Notice --> DB
        Blast --> DB
        Audit --> DB
    end

    Reader --> ReadingSubmit["submit_reader_reading()<br/>update_reader_reading()"]
    Admin --> ReadingSubmit
    ReadingSubmit --> ReadingService["handle_meter_reading_submission()"]
    ReadingService --> Reading
    ReadingService --> BillingSync["create_or_update_billing_from_reading()"]
    BillingSync --> Billing
    ReadingService --> ReadingNotify["notify_roles() + notify_consumer()"]
    ReadingNotify --> Notice

    Admin --> ConsumerMgmt["consumer_list()<br/>add_consumer()<br/>edit_consumer()"]
    ConsumerMgmt --> ConsumerRecord

    Admin --> ManualBilling["add_billing()"]
    ManualBilling --> Billing
    ManualBilling --> DueNotify["send_billing_due_notification()"]
    DueNotify --> Notice

    Consumer --> ConsumerPayment["ConsumerPaymentForm -> payment save"]
    ConsumerPayment --> PaymentMethod{"Payment method"}
    PaymentMethod --> CashPath["Cash / Manual"]
    PaymentMethod --> OnlinePath["Online / E-wallet"]

    CashPath --> Payment
    CashPath --> PaymentNotify["send_payment_notification()"]

    OnlinePath --> PayMongoStart["create_paymongo_ewallet_payment()"]
    PayMongoStart --> PayMongo["PayMongo API"]
    PayMongo --> Verify["paymongo_verify()"]
    Verify --> PaymentStatus{"Paid?"}
    PaymentStatus -->|Yes| Complete["update_payment_status(completed)"]
    PaymentStatus -->|No / Failed| Failed["update_payment_status(failed)"]
    Complete --> Payment
    Failed --> Payment
    Complete --> Billing
    Failed --> Billing
    Complete --> PaymentNotify
    Failed --> PaymentNotify
    PaymentNotify --> Notice

    Admin --> StaffPayments["payments_list()<br/>update_payment_status_view()"]
    Treasurer --> StaffPayments
    Secretary --> StaffPayments
    StaffPayments --> Payment
    StaffPayments --> Billing
    StaffPayments --> Audit
    StaffPayments --> PaymentNotify

    Admin --> Reports["reports_view()<br/>reports_export_view()"]
    Secretary --> Reports
    Treasurer --> Reports
    Reports --> ReportData["Aggregate Billing + Payment + Reading data"]
    ReportData --> SOA["Statement of Account PDF"]

    Admin --> Communications["communications_view()"]
    Secretary --> Communications
    Communications --> Blast
    Communications --> Notice

    Admin --> PaymentSettings["payment_settings_view()"]
    PaymentSettings --> Settings
    PaymentSettings --> BillingRecalc["sync_existing_billings_with_settings()"]
    BillingRecalc --> Billing
    PaymentSettings --> Audit

    Consumer --> Account["account_center()"]
    Consumer --> NotificationView["notifications_view()"]
    NotificationView --> Notice
    Consumer --> ProfileView["profile_view()<br/>update_profile_view()"]

    ReadingNotify --> Delivery["Email / SMS / In-app delivery"]
    DueNotify --> Delivery
    PaymentNotify --> Delivery
    Communications --> Delivery
```

## Main System Flow

1. Users authenticate and are redirected to a role-based dashboard.
2. Admin and reader accounts submit meter readings.
3. Meter readings create or update billing records using `SystemSettings`.
4. Consumers or staff create payments.
5. Payment saves refresh billing balances and statuses.
6. Notifications are sent through in-app records and optional email/SMS providers.
7. Admin, secretary, and treasurer dashboards read aggregated billing, payment, and meter data for reports.
