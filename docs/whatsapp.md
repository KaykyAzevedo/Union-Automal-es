# Avisos no WhatsApp (CallMeBot)

O painel manda uma mensagem no seu WhatsApp quando:

- entra um **carro novo** no site: `🚗 Carro novo: Chevrolet TRACKER 1.0 TURBO FLEX LTZ AUTOMÁTICO 2026 — R$ 129.900,00`, com o link para abrir o encarte do carro;
- um carro é **vendido** (sumiu do site): `🔴 Vendido: <nome do carro>`;
- algum carro **baixa de preço** (revisão das 18h).

Se entrarem mais de 3 carros de uma vez, chega uma mensagem só com a lista.

O envio é feito pelo **CallMeBot**, um serviço gratuito que manda mensagens para o **seu próprio número**. Ele não manda mensagem para clientes nem para grupos.

---

## Passo 1: pegar a sua chave (apikey)

Faça isto no celular onde está o WhatsApp que vai receber os avisos.

1. **Salve este contato** no celular: **+34 644 95 42 75** (pode dar o nome "CallMeBot").
   > O número pode mudar. Se não funcionar, confira o número atual em
   > https://www.callmebot.com/blog/free-api-whatsapp-messages/
2. Abra o WhatsApp, entre na conversa com esse contato e **mande exatamente esta frase**:

   ```
   I allow callmebot to send me messages
   ```

3. Em até 2 minutos chega uma resposta (em inglês) com a sua **APIKEY**, um número como `123456`.
   **Anote esse número: é a sua apikey.**
4. Se a resposta não chegar em 2 minutos, espere 24 horas e mande a frase de novo.

## Passo 2: testar no navegador (opcional)

Cole no navegador, trocando o seu número (com 55 e DDD, sem espaços) e a sua apikey:

```
https://api.callmebot.com/whatsapp.php?phone=+5521999999999&text=Teste+Union&apikey=123456
```

Em alguns segundos deve chegar "Teste Union" no seu WhatsApp.

## Passo 3: colocar no app

### Na Vercel (app na nuvem)

Em **Vercel → seu projeto → Settings → Environment Variables**, crie:

| Nome | Valor | Exemplo |
|---|---|---|
| `WHATSAPP_PHONE` | seu número com 55 e DDD, só números | `5521999999999` |
| `CALLMEBOT_APIKEY` | a apikey que chegou no WhatsApp | `123456` |
| `APP_URL` | endereço do painel (usado no link das mensagens) | `https://union-painel.vercel.app` |

Depois clique em **Redeploy** para as variáveis valerem.

### No computador (app local)

Defina as mesmas variáveis antes de abrir o app. No PowerShell:

```powershell
$env:WHATSAPP_PHONE = "5521999999999"
$env:CALLMEBOT_APIKEY = "123456"
```

Se as variáveis não existirem, o app funciona normalmente, só sem WhatsApp. No computador o aviso no canto da tela (Windows) continua aparecendo.

---

## Problemas comuns

- **Não chega nada:** confira se `WHATSAPP_PHONE` tem 55 + DDD e se a apikey está certa (teste o Passo 2). Veja também o log do app: aparece `WhatsApp: CallMeBot recusou` ou `WhatsApp: falha de envio`.
- **"APIKey is invalid":** a apikey está errada ou é de outro número. Refaça o Passo 1.
- **Mensagem atrasada:** o CallMeBot é gratuito e às vezes demora alguns minutos. Se o serviço estiver fora do ar, o app continua funcionando, só o aviso não chega.
- **Trocou de número de WhatsApp:** refaça o Passo 1 com o número novo e atualize as duas variáveis.

O CallMeBot é gratuito e o próprio serviço diz que é para uso pessoal. A chave dá acesso só a mandar mensagens para o seu número; mesmo assim, não a compartilhe.
