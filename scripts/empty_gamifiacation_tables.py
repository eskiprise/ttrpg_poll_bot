import boto3

REGION = "eu-west-2"
ENV = "dev"  # or "prod" — run once per environment you already backfilled

dynamodb = boto3.resource("dynamodb", region_name=REGION)
for suffix in ["telegram_xp_ledger", "telegram_player_level", "telegram_achievements"]:
    table = dynamodb.Table(f"ttrpg_club_{ENV}_{suffix}")
    key_names = [k["AttributeName"] for k in table.key_schema]
    deleted = 0
    scan_kwargs = {}
    while True:
        response = table.scan(**scan_kwargs)
        with table.batch_writer() as batch:
            for item in response.get("Items", []):
                batch.delete_item(Key={k: item[k] for k in key_names})
                deleted += 1
        if "LastEvaluatedKey" not in response:
            break
        scan_kwargs["ExclusiveStartKey"] = response["LastEvaluatedKey"]
    print(f"{table.table_name}: deleted {deleted} items")