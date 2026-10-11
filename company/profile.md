# Acme Corp

Acme Corp is a mid-sized software company. TaskPilot works for the Finance (Accounts Payable) and Customer Support teams.

- The shared mailbox (Mail) receives vendor invoices in the "Accounts Payable" folder and customer emails in the "Support" folder.
- The ERP is the system of record for vendors and vendor invoices. Sign in as `ap.agent` with the `erp_password` secret.
- The Helpdesk is where customer support tickets are tracked.
- All amounts are in INR unless an invoice says otherwise. Indian digit grouping (e.g. 2,40,000) is common.
- "Latest" means most recently received, judged by the email's received date.
