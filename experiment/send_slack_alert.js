const { WebClient } = require('@slack/web-api');

// Slack token and channel
const token = process.env.SLACK_TOKEN;
const channel = process.env.SLACK_CHANNEL;

// Initialize Slack client
const web = new WebClient(token);

// Function to send alert to Slack
async function sendSlackAlert(message) {
  try {
    await web.chat.postMessage({
      channel: channel,
      text: message,
    });
    console.log('Alert sent to Slack successfully.');
  } catch (error) {
    console.error('Error sending alert to Slack:', error);
  }
}

// Export the function
module.exports = { sendSlackAlert };
