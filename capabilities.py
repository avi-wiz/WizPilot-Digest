"""Shipped WizPilot capabilities, for the daily failure audit.

Why this exists: a failed question that maps to a capability we have ALREADY
SHIPPED is a regression or a bug — someone should fix it this week. A failed
question with no matching capability is a roadmap gap — it goes on a backlog.
The audit can't tell those apart without knowing what's live, so the list is
passed to the LLM and it tags each theme.

Keep this in sync as capabilities ship. Format is "ID: name" for user stories;
UI components and platform features have no ID.
"""

CAPABILITIES = """\
US-01: Product Search
US-03: Similar / Alternative Product Recommendations
US-04: Inventory & Stock Level Lookup
US-06: Customer & Lead Summary (360°)
US-07: Meeting Preparation
US-09: Create / Edit / Delete CRM Entities
US-11: In-Person Visit Logging
US-12: Create / View / Update / Delete Sales Entities
US-13: Repeat Previous Orders
US-14: Cart Building & Management
US-15: Fetch Order / Invoice / Shipment Status
US-16: Payment Transactions Lookup
US-17: Catalog Generation
US-20: Personalised Email Drafting
US-21: Abandoned-Cart Recovery Emails
US-23: Kai Persona & Voice Configuration (Voice to Text only)
US-42: Business Performance Monitoring
US-43: Persistent Dashboard Creation
US-51: Publish AI Reports, Reports & Dashboards
US-54: Knowledge Base Document Upload
US-56: Memory Layer
US-57: User Preferences & Personalisation
US-58: Screen-Aware Contextual Intelligence
US-59: Mobile & Tablet Accessibility
Product Card (Dynamic with custom attributes)
Chain-of-Thought Card
Status Badge
Navigation / Consent Widget
Customer360 Widget
Data Table (with cross-entity columns, filters, sort)
Filter Chip Bar Widget
Metric Card
Line Chart
Bar Chart
Pie Chart
Ask Kai CTA in Top Nav Bar
Kai Side Menu (Chat, History, Reports, KB, Preferences, Admin)
Trace Logging (query -> outcome)
Alerting (Slack alerts on breaches)
Kai Dashboard - Langsmith
AP Portal Configuration to control Kai's Data Sources and Capabilities"""
