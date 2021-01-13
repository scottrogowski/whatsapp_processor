## WhatsApp export processor.

Use this to convert your raw WhatsApp coversation exports into structured json. This works for conversations exported to Google Drive. Use this for:

1. [Fleeing WhatsApp for Telegram / Signal](https://arstechnica.com/tech-policy/2021/01/whatsapp-users-must-share-their-data-with-facebook-or-stop-using-the-app/)
2. Archiving WhatsApp conversations
3. Research (what this was originally designed for)

## Extra features:
- **Obfuscation**: If using for research, an --obfuscation-key parameter is available to remove personally identifiable information (PII) from the conversation. So names, phone numbers, and group names will be encrypted.
- **Smart multiple export merges**: Export to the same folder twice without getting duplicate messages


### To export your chats

1. Click the top right dot menu of your WhatsApp conversation.
2. Go to More -> Export chat
3. Choose either with or without media
4. Choose the "Drive: Save to Drive" option. 
5. IMPORTANT: Move your export to a google drive folder

### Installation

Download this repo and then run

    pip3 install -r requirements.txt

### Authentication

There are two options for authentication. Both generate a json file which needs to be passed in:
1. Individual account option: 
    Go here on your google account and enable the drive API:
    https://developers.google.com/drive/api/v3/quickstart/python
    This will give you a credentials.json file.
2. Service account option:
    Create a service account here:
    https://console.developers.google.com/iam-admin/serviceaccounts
    After creating, click "create key" on the tab to right and download.
    IMPORTANT: You will then need to share the drive directory with the service account email.

### Basic script usage

    ./whatsapp_processor.py path/to/creds.json drive.google.com/folders/drive_id MDY --verbose

For help on the CLI arguments, try `./whatsapp_processor.py --help`

### Acknowledgements

This was adapted from code I wrote for [Tattle](https://tattle.co.in/). Tattle is a civic tech project that builds tools and datasets to better understand and respond to (mis)information trends in India. The original code can be found [here](https://github.com/tattle-made/whatsapp-scraper/tree/master/python_scraper).


### License

[MIT License](https://opensource.org/licenses/MIT)

### Testing

    ./test.sh

The test.sh file uses the coverage python module. You can see the code coverage with

    firefox htmlcov/index.html

Get off of chrome ya bums.
