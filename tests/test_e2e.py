"""Fluxo completo pelo HTTP: baseline → carro novo → chamado → feito → queda → alerta → dispensar."""
from __future__ import annotations


def test_full_flow(client, site, car):
    # 1) primeira execução: baseline, sem chamados
    site.cars = [car("1", "Onix 1.0 LT 2020", 7_000_000), car("2", "HB20 1.6 2019", 6_000_000)]
    assert client.post("/admin/check-now", follow_redirects=False).status_code == 303
    stats = client.get("/health").json()["stats"]
    assert stats == {**stats, "pending": 0, "total_cars": 2, "alerts": 0}
    assert stats["last_check"] is not None
    assert "Tudo em dia" in client.get("/").text

    # 2) carro novo aparece → 1 chamado (poll repetido não duplica)
    site.cars.append(car("3", "Jeep Renegade Longitude 2021", 11_000_000))
    client.post("/admin/check-now")
    client.post("/admin/check-now")
    html = client.get("/").text
    assert "Jeep Renegade Longitude 2021" in html
    assert "https://img.test/3.jpg" in html
    assert "R$ 110.000,00" in html
    assert client.get("/health").json()["stats"]["pending"] == 1

    # 3) Feito
    r = client.post("/tickets/1/done")
    assert r.status_code == 200 and "Postado" in r.text
    assert "Jeep Renegade Longitude 2021" not in client.get("/").text
    assert "Postado" in client.get("/cars").text

    # 4) preço cai (e outro sobe) → só 1 alerta; rodar 2x não duplica
    site.cars = [car("1", "Onix 1.0 LT 2020", 6_500_000), car("2", "HB20 1.6 2019", 6_200_000),
                 car("3", "Jeep Renegade Longitude 2021", 11_000_000)]
    client.post("/admin/price-check-now")
    client.post("/admin/price-check-now")
    html = client.get("/").text
    assert "R$ 70.000,00" in html and "R$ 65.000,00" in html
    assert "HB20 1.6 2019" not in html  # subiu: sem alerta
    assert client.get("/health").json()["stats"]["alerts"] == 1

    # 5) dispensar
    r = client.post("/alerts/1/dismiss")
    assert r.status_code == 200 and r.text == ""
    assert client.get("/health").json()["stats"]["alerts"] == 0
    assert "Onix 1.0 LT 2020" not in client.get("/").text

    # 6) carro sai do site: 1 coleta sem ele não basta; 2 seguidas → vendido
    site.cars = site.cars[1:]
    client.post("/admin/check-now")
    stats = client.get("/health").json()["stats"]
    assert stats["total_cars"] == 3 and stats["sold"] == 0
    client.post("/admin/check-now")
    stats = client.get("/health").json()["stats"]
    assert stats["total_cars"] == 2 and stats["sold"] == 1 and stats["pending"] == 0
    html = client.get("/").text
    assert "Onix 1.0 LT 2020" in html and 'hx-post="/sold/1/dismiss"' in html
    assert "Vendido" in client.get("/cars").text

    # 7) dispensar o aviso de vendido
    r = client.post("/sold/1/dismiss")
    assert r.status_code == 200 and r.text == ""
    assert client.get("/health").json()["stats"]["sold"] == 0
    assert "Onix 1.0 LT 2020" not in client.get("/").text
