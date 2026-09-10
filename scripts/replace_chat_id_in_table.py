import boto3
import time
from decimal import Decimal

TABLE_NAME = "ttrpg_club_prod_telegram_rating_polls"
CHAT_ID_ATTR = "chatId"          # <-- confirmed from results.csv header
OLD_CHAT_ID = 2578567898
NEW_CHAT_ID = -1002578567898
REGION = "eu-west-2"

DRY_RUN = False  # <-- set to False to actually write/delete

def log(msg):
    ts = time.strftime("%H:%M:%S")
    print(f"[{ts}] {msg}")

log("Starting script")
log(f"Table: {TABLE_NAME}")
log(f"Region: {REGION}")
log(f"Mode: {'DRY RUN (no writes will happen)' if DRY_RUN else 'LIVE RUN (changes will be applied)'}")
log(f"Chat ID attribute name: {CHAT_ID_ATTR}")
log(f"OLD_CHAT_ID = {OLD_CHAT_ID}")
log(f"NEW_CHAT_ID = {NEW_CHAT_ID}")

dynamodb = boto3.resource("dynamodb", region_name=REGION)
client = boto3.client("dynamodb", region_name=REGION)
table = dynamodb.Table(TABLE_NAME)

log("Fetching table schema via describe_table...")
desc = client.describe_table(TableName=TABLE_NAME)["Table"]
key_names = [k["AttributeName"] for k in desc["KeySchema"]]
key_roles = {k["AttributeName"]: k["KeyType"] for k in desc["KeySchema"]}
attr_types = {a["AttributeName"]: a["AttributeType"] for a in desc["AttributeDefinitions"]}
item_count_estimate = desc.get("ItemCount", "unknown (may be stale)")

log(f"Table status: {desc.get('TableStatus')}")
log(f"Key schema: {[(k, key_roles[k]) for k in key_names]}")
log(f"Attribute definitions: {attr_types}")
log(f"Approx item count (from table metadata, may lag): {item_count_estimate}")

chat_id_is_key = CHAT_ID_ATTR in key_names
chat_id_type = attr_types.get(CHAT_ID_ATTR, "N")  # default assume Number

if chat_id_is_key:
    log(f"{CHAT_ID_ATTR} IS part of the key schema (role: {key_roles[CHAT_ID_ATTR]}) -> will use DELETE + PUT strategy")
else:
    log(f"{CHAT_ID_ATTR} is a plain (non-key) attribute -> will use UPDATE strategy")
log(f"{CHAT_ID_ATTR} DynamoDB type: {chat_id_type}")

def cast(value):
    if chat_id_type == "N":
        return Decimal(str(value))
    return str(value)

old_val = cast(OLD_CHAT_ID)
new_val = cast(NEW_CHAT_ID)
log(f"Casted OLD value: {old_val!r} ({type(old_val).__name__})")
log(f"Casted NEW value: {new_val!r} ({type(new_val).__name__})")

log("-" * 70)
log("Beginning scan for matching items...")

scanned = 0
matched = 0
would_update = 0
updated = 0
errors = 0
last_evaluated_key = None
page_num = 0

while True:
    page_num += 1
    scan_kwargs = {
        "FilterExpression": f"{CHAT_ID_ATTR} = :old",
        "ExpressionAttributeValues": {":old": old_val},
    }
    if last_evaluated_key:
        scan_kwargs["ExclusiveStartKey"] = last_evaluated_key

    log(f"Scanning page {page_num}" + (f" (continuing from key {last_evaluated_key})" if last_evaluated_key else " (first page)"))
    response = table.scan(**scan_kwargs)
    items = response.get("Items", [])
    page_scanned_count = response.get("ScannedCount", len(items))
    scanned += page_scanned_count
    matched += len(items)

    log(f"Page {page_num}: scanned {page_scanned_count} items in this page, {len(items)} matched filter")

    for idx, item in enumerate(items, start=1):
        key = {k: item[k] for k in key_names}
        log(f"  Item {idx}/{len(items)} on page {page_num} | key={key}")
        log(f"    Full item before change: {item}")

        if DRY_RUN:
            would_update += 1
            if chat_id_is_key:
                new_key_preview = dict(key)
                new_key_preview[CHAT_ID_ATTR] = new_val
                log(f"    [DRY RUN] Would DELETE item with key={key}")
                log(f"    [DRY RUN] Would PUT new item with key={new_key_preview} ({CHAT_ID_ATTR} changed {old_val} -> {new_val})")
            else:
                log(f"    [DRY RUN] Would UPDATE item key={key}: {CHAT_ID_ATTR} {old_val} -> {new_val}")
            continue

        try:
            if chat_id_is_key:
                old_key = key
                new_item = dict(item)
                new_item[CHAT_ID_ATTR] = new_val

                log(f"    Writing new item with key={ {k: new_item[k] for k in key_names} }...")
                table.put_item(Item=new_item)
                log(f"    Put succeeded. Deleting old item with key={old_key}...")
                table.delete_item(Key=old_key)
                log(f"    Delete succeeded.")
            else:
                log(f"    Updating item key={key}: {CHAT_ID_ATTR} {old_val} -> {new_val}...")
                table.update_item(
                    Key=key,
                    UpdateExpression=f"SET {CHAT_ID_ATTR} = :new",
                    ExpressionAttributeValues={":new": new_val},
                )
                log(f"    Update succeeded.")
            updated += 1
        except Exception as e:
            errors += 1
            log(f"    ERROR processing item key={key}: {e}")

    last_evaluated_key = response.get("LastEvaluatedKey")
    if not last_evaluated_key:
        log("No more pages to scan.")
        break
    else:
        log(f"More pages remain, next ExclusiveStartKey={last_evaluated_key}")

log("-" * 70)
log("SUMMARY")
log(f"Total items scanned (all pages, before filter): {scanned}")
log(f"Total items matched OLD_CHAT_ID: {matched}")
if DRY_RUN:
    log(f"Would update: {would_update}")
    log("This was a DRY RUN — no data was changed. Set DRY_RUN = False to apply.")
else:
    log(f"Successfully updated: {updated}")
    log(f"Errors encountered: {errors}")
log("Done.")