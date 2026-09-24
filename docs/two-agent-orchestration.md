# Uzasadnienie architektury dwóch agentów

## Podział odpowiedzialności

| Agent | Uprawnienia | Wynik |
| --- | --- | --- |
| Diagnostyczny | Może korzystać z wyszukiwania internetowego oraz z danych tylko do odczytu z AKS. | Ustrukturyzowane fakty, ocena pewności i pojedyncza propozycja naprawy. |
| Remediacyjny | Nie ma przeglądarki, wyszukiwania ani zależności od zewnętrznych wyników. Dostaje wyłącznie kontrakt Facts. | Samodzielnie autoryzuje operację, wykonuje ją i sprawdza rollout. |

## Dlaczego tak

Wynik wyszukiwania internetowego jest przydatny do diagnozy, ale jest zewnętrznym i nieufnym wejściem. Może być nieaktualny, błędny albo zawierać instrukcje nieistotne dla incydentu. Dlatego pierwszy agent nie dostaje dostępu do wykonawcy ani prawa wyboru namespace, Deploymentu lub kontenera.

Drugi agent jest celowo węższy. Nie korzysta z sieci ani z narzędzi researchu, więc nie może rozszerzyć zakresu zadania pod wpływem strony internetowej lub odpowiedzi modelu. Weryfikuje lokalne fakty i reguły bezpieczeństwa: dozwolony rejestr, małą różnicę nazwy obrazu, istnienie poprawionego obrazu w ACR oraz właściwy cel w AKS.

## Warunek zakończenia

Incydent ma status resolved wyłącznie wtedy, gdy drugi agent pomyślnie autoryzuje operację, ją wykona i niezależnie potwierdzi zdrowy rollout. Każda odmowa polityki, błąd wykonania, błąd infrastruktury albo nieudana weryfikacja drugiego agenta kończy incydent jako escalated z powodem i dowodami do analizy przez człowieka.
