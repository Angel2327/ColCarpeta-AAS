# ColCarpeta en minikube (evidencia de varias réplicas)

Despliegue **adicional**, solo para demostrar que la aplicación corre con varias
réplicas al mismo tiempo. El despliegue público real sigue siendo Railway
(`https://colcarpeta-aas-production.up.railway.app`); esto no lo reemplaza, no lo
toca, y no corre ninguna migración ni escritura contra la base de datos real de
Supabase -- los manifiestos apuntan a la base que ya existe, tal como está.

**Nota sobre versiones de `kubectl`.** El `kubectl` local de esta máquina es 1.36.1
contra un servidor (minikube) 1.31.0 -- un desfase mayor al soporte habitual de ±1
versión menor. En la práctica, durante todo este despliegue (crear el Secret, aplicar
manifiestos, `rollout restart`, `delete pod`, `port-forward`, `logs`) **ningún comando
falló por esto** -- solo aparece una advertencia de version skew, nunca un error. Por
eso todos los comandos de abajo son `kubectl` normal; si algún día un comando sí
falla por la diferencia de versión, la alternativa es `minikube kubectl -- <comando>`
(usa el `kubectl` empaquetado con minikube, en la versión del servidor).

**Nota sobre `minikube service --url`.** Con el driver `docker` en Windows, ese
comando necesita una terminal abierta (crea un túnel y se queda corriendo) -- no
funciona bien para scripts no interactivos. Por eso todas las verificaciones de abajo
usan `kubectl port-forward` en su lugar, que sí funciona sin depender de una terminal
interactiva.

## 0. Qué ya quedó hecho en esta sesión

Todo lo de las secciones 1 a 5 de abajo **ya se ejecutó una vez** y el clúster está
corriendo en este momento con 3 réplicas sanas. Las secciones están igual escritas en
orden, de principio a fin, para que se puedan repetir tal cual si se reinicia
minikube, se borra el Deployment, o se quiere mostrar el proceso completo en la
entrega. Cada comando dice explícitamente si **ya lo ejecuté yo** (y con qué
resultado) o si queda para que lo corras **vos**.

## 1. Cambio de código (ya aplicado, antes de todo esto)

`app/db.py` ya no crea el motor de SQLAlchemy sin límite de conexiones -- ahora usa
`DB_POOL_SIZE`/`DB_MAX_OVERFLOW` (nuevas en `app/config.py`, default 3+2 por proceso).
Sin esto, 3 réplicas podían abrir hasta 45 conexiones contra Supabase (15 cada una,
el default de SQLAlchemy). Ver el reporte de la tarea para el diff exacto. Esto ya
está en el código, no hay nada que correr para esta parte.

## 2. Generar el Secret desde el `.env` real -- YA LO EJECUTÉ YO

El Secret (`colcarpeta-secret`) ya existe en el clúster, generado una sola vez a
partir del `.env` real local. **Esto es sensible: si hace falta regenerarlo (por
ejemplo, tras rotar una credencial), lo corrés VOS, no yo, para no tener que pasarte
las credenciales reales por el chat.**

Problema a evitar: `kubectl create secret generic --from-env-file=` lee el archivo
**literal**, sin quitar comillas ni interpretar `\n` como salto de línea. El `.env`
real tiene `JWT_LLAVE_PRIVADA`/`JWT_LLAVE_PUBLICA` entre comillas dobles -- pasarle el
`.env` tal cual deja esas comillas como parte del valor y la aplicación falla de forma
confusa al intentar parsear el PEM. Por eso se genera antes un archivo intermedio,
solo con las 6 claves sensibles, con las comillas ya quitadas (usando
`python-dotenv`, el mismo parser que ya usa `pydantic-settings` para leer el `.env`
real) -- y ese archivo intermedio **nunca** se versiona ni queda en el repositorio.

```bash
# (VOS, solo si hace falta regenerar el Secret) -- corré esto desde la raíz del repo,
# con el entorno del proyecto activado:
python - <<'PY'
from pathlib import Path
from dotenv import dotenv_values

ENV_REAL = Path(".env")
SALIDA = Path("/ruta/fuera/del/repo/colcarpeta-secret.env")  # NUNCA dentro del repo

CLAVES_SENSIBLES = [
    "DATABASE_URL", "S3_ACCESS_KEY", "S3_SECRET_KEY",
    "JWT_LLAVE_PRIVADA", "JWT_LLAVE_PUBLICA", "ADMIN_PASSWORD_HASH",
]
valores = dotenv_values(ENV_REAL)
with open(SALIDA, "w", encoding="utf-8", newline="\n") as f:
    for clave in CLAVES_SENSIBLES:
        valor = (valores.get(clave) or "").replace("\n", "\\n")
        f.write(f"{clave}={valor}\n")
print(f"Escrito: {SALIDA}")
PY

kubectl create secret generic colcarpeta-secret --from-env-file=/ruta/fuera/del/repo/colcarpeta-secret.env

# Verificar que ningun valor quedo con comillas de mas (compara con el .env real):
kubectl get secret colcarpeta-secret -o jsonpath='{.data.JWT_LLAVE_PRIVADA}' | base64 -d
kubectl get secret colcarpeta-secret -o jsonpath='{.data.DATABASE_URL}' | base64 -d

# Borrar el archivo intermedio despues de usarlo -- no debe quedar en el disco.
rm /ruta/fuera/del/repo/colcarpeta-secret.env
```

`k8s/secret.example.yaml` es solo una plantilla versionable (valores de relleno) --
documenta las 6 claves que tiene que traer el Secret real, nunca se aplica
directamente con `kubectl apply`.

## 3. ConfigMap, Deployment y Service -- YA LOS APLIQUÉ YO

```bash
# YA EJECUTADO POR MÍ, con este resultado:
#   configmap/colcarpeta-config created
#   deployment.apps/colcarpeta created
#   service/colcarpeta created
kubectl apply -f k8s/configmap.yaml -f k8s/deployment.yaml -f k8s/service.yaml
```

Si se quiere repetir desde cero (por ejemplo tras un `kubectl delete -f k8s/` o un
`minikube delete`), este mismo comando sirve para volver a crearlos -- es idempotente,
`kubectl apply` solo actualiza lo que cambió si ya existen.

## 4. Construir la imagen y cargarla en minikube -- YA LO HICE YO

```bash
# YA EJECUTADO POR MÍ.
docker build -t colcarpeta:local .

# YA EJECUTADO POR MÍ: tardó ~23-32s, tiempo razonable -- no hizo falta usar
# "minikube image build" como alternativa (la instrucción pedía avisar si se usaba;
# no se usó).
minikube image load colcarpeta:local
```

## 5. Verificación -- YA LA HICE YO (podés repetirla vos para confirmar)

**5.1 -- Los 3 pods en `Running` y `Ready`:**

```bash
kubectl get pods -o wide
```

Resultado real, con el clúster corriendo ahora mismo:

```
NAME                          READY   STATUS    RESTARTS   AGE   IP            NODE       NOMINATED NODE   READINESS GATES
colcarpeta-58d6d6898c-7zqnk   1/1     Running   0          81s   10.244.0.10   minikube   <none>           <none>
colcarpeta-58d6d6898c-gmb9t   1/1     Running   0          71s   10.244.0.11   minikube   <none>           <none>
colcarpeta-58d6d6898c-xns8k   1/1     Running   0          60s   10.244.0.12   minikube   <none>           <none>
```

```bash
kubectl get svc colcarpeta
```

```
NAME         TYPE       CLUSTER-IP       EXTERNAL-IP   PORT(S)          AGE
colcarpeta   NodePort   10.111.250.191   <none>        8000:31533/TCP   8m14s
```

**5.2 y 5.3 -- `GET /health` responde 200 a través del Service, y el portal carga
en la raíz (sin registrar a nadie):**

```bash
kubectl port-forward svc/colcarpeta 18000:8000 &

curl -i http://127.0.0.1:18000/health
# HTTP/1.1 200 OK
# {"status":"ok","operador":"ColCarpeta","operator_id":"6aaeb415b765590002607402","version":"0.1.0"}

curl -i http://127.0.0.1:18000/
# HTTP/1.1 303 See Other -> Location: /sesion (correcto: sin sesion, el portal
# redirige a iniciar sesion; no se registro ni se autentico a nadie)

curl -i -L http://127.0.0.1:18000/
# HTTP/1.1 200 OK, content-type text/html -- la pantalla de inicio de sesion real
```

**5.4 -- Rolling update sin caída:** cambié una línea trivial (un campo nuevo en la
respuesta de `/health`, solo para esta prueba, revertido después), reconstruí,
recargué la imagen, y disparé el reinicio mientras un script pedía `/health` cada
0.5 segundos:

```bash
# (cambio trivial en app/main.py, luego:)
docker build -t colcarpeta:local .
minikube image load colcarpeta:local
kubectl rollout restart deployment/colcarpeta
kubectl rollout status deployment/colcarpeta --timeout=120s
```

Resultado real: **120 de 120 peticiones a `/health` respondieron 200**, sin ninguna
falla, durante los ~106 segundos que tardó el `rollout restart` completo (verificado
con las marcas de tiempo del propio registro, de `13:12:25` a `13:14:11`). El cambio
trivial se revirtió después y se volvió a reconstruir/recargar/reiniciar para dejar el
clúster corriendo la imagen real, sin ese campo de prueba -- confirmado comparando
`/health` antes y después del revertido.

**5.5 -- Borrar un pod a mano, Kubernetes lo recrea solo:**

```bash
kubectl delete pod $(kubectl get pods -l app=colcarpeta -o jsonpath='{.items[0].metadata.name}')
kubectl get pods -o wide
```

Resultado real: el pod borrado (`colcarpeta-7dd8c799d6-9pnc2`) desapareció y, unos
segundos después, apareció un pod nuevo (`colcarpeta-7dd8c799d6-bcxps`) en estado
`Running`/`1/1` -- el Deployment mantiene siempre 3 réplicas sin que nadie tenga que
volver a crearlo a mano.

**Si algo falla:** `kubectl logs <nombre-del-pod>` -- durante esta sesión los 3 pods
levantaron limpio en el primer intento, sin ningún error que corregir.

## 6. Apagar todo -- QUEDA PARA VOS

Destructivo (borra pods, servicio, config y el Secret con las credenciales reales) --
por eso no lo corrí yo, lo dejo para que lo ejecutes cuando quieras terminar la
demostración:

```bash
kubectl delete -f k8s/deployment.yaml -f k8s/service.yaml -f k8s/configmap.yaml
kubectl delete secret colcarpeta-secret

# Opcional, libera tambien la imagen cargada en minikube:
docker exec minikube crictl rmi colcarpeta:local 2>/dev/null || true

# Opcional, mas drastico: apaga minikube por completo (no solo este despliegue).
# NO lo corras si usás minikube para otra cosa.
minikube stop
```
