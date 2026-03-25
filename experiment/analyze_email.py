import openai

# Function to analyze email content

def analyze_email_content(email_body):
    response = openai.ChatCompletion.create(
        model="gpt-4",
        messages=[
            {
                "role": "user",
                "content": f"Analyze the following email content and determine if it is a support request: {email_body}"
            }
        ],
        max_tokens=100
    )
    return response['choices'][0]['message']['content']

# Example email body
email_body = "I am having trouble accessing my account. Can you help me?"

# Analyze the email
result = analyze_email_content(email_body)
print(result)