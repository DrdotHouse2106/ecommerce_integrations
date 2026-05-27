def main():
    import importlib
    mod = importlib.import_module("ecommerce_integrations.product_sync.engine.adapters.shopware")
    def q(client):
        emp = client.request_post("search-ids/media", payload={
            "limit":1, "total-count-mode":1,
            "filter":[
                {"type":"range","field":"createdAt","parameters":{"gte":"2026-05-26T19:12:00"}},
                {"type":"equals","field":"fileName","value":None},
            ],
        })
        pop = client.request_post("search-ids/media", payload={
            "limit":1, "total-count-mode":1,
            "filter":[
                {"type":"range","field":"createdAt","parameters":{"gte":"2026-05-26T19:12:00"}},
                {"type":"not","operator":"and","queries":[{"type":"equals","field":"fileName","value":None}]},
            ],
        })
        print(f"since 22828 start (19:12):  empty={emp.get('total')} populated={pop.get('total')}")
    mod._with_client(q)
