import imaplib
import email
from email.header import decode_header
import time

# Account credentials
username = "your_email@example.com"
password = "your_password"

# Connect to the server
mail = imaplib.IMAP4_SSL("imap.example.com")

# Login to your account
mail.login(username, password)

# Select the mailbox you want to check
mail.select("inbox")

# Function to check for new emails
def check_for_new_email():
    # Search for all emails
    status, messages = mail.search(None, "ALL")
    # Convert messages to a list of email IDs
    email_ids = messages[0].split()
    # Get the latest email ID
    latest_email_id = email_ids[-1]
    # Fetch the email by ID
    status, msg_data = mail.fetch(latest_email_id, "(RFC822)")
    # Get the email content
    msg = email.message_from_bytes(msg_data[0][1])
    # Decode the email subject
    subject, encoding = decode_header(msg["Subject"])[0]
    if isinstance(subject, bytes):
        # If it's a bytes, decode to str
        subject = subject.decode(encoding if encoding else "utf-8")
    # Print the subject
    print("New Email: ", subject)

# Periodically check for new emails
while True:
    check_for_new_email()
    # Wait for 60 seconds before checking again
    time.sleep(60)
