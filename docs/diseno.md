# Dirección visual del portal

Especificación para aplicar al portal del ciudadano. Todo lo que sigue se expresa
contra las clases que ya existen en `app/portal/templates/` y en
`app/portal/static/estilos.css`: es un cambio de piel, no una reescritura.

## 1. Principios

- **El fondo de página es papel cálido, no gris azulado.** Es lo que más separa un
  portal público de un formulario interno sin diseñar.
- **Dos familias tipográficas con intención.** Un serif institucional para títulos y
  la marca; un sans de administración pública para el texto. Hoy se usa la fuente
  del sistema, que no dice nada.
- **Certificado y temporal tienen que verse distintos de un vistazo**, porque es la
  distinción central del caso de estudio. Hoy `etiqueta--certificado` y
  `etiqueta--firma-valida` comparten el verde y se confunden.
- **El rojo queda reservado para lo irreversible**: trasladarse y eliminar. Nada más.
- **Los tres estados de firma se distinguen por texto, no solo por color**: firma
  verificada, firma no verificada, sin firma digital.
- **Lo que viene de un sistema externo se traduce antes de mostrarse.** Un
  certificado X.509, un código de país, el estado de una API ajena: nada de eso
  llega en un formato pensado para una persona, y mostrarlo tal cual es jerga, no
  información. El dato crudo se conserva -- alguien puede necesitar comprobarlo --
  pero no es lo que se enseña primero: va accesible, nunca destacado (ver "Datos que
  vienen de un sistema externo", sección 5).
- **Una etiqueta de estado no puede ser una palabra que la persona también pueda
  escribir como dato.** Si puede, se nombra la acción o el actor detrás del estado, no
  la cualidad -- ver "Etiquetas de estado", sección 4, con el caso real que motivó
  esta regla.

## 2. Variables

Reemplaza el bloque `:root` de `estilos.css` por este:

```css
:root {
  --color-fondo-pagina: #F7F5F0;   /* papel: fondo de la página */
  --color-fondo: #FFFFFF;          /* superficie: tarjetas y campos */
  --color-texto: #1B1D1A;
  --color-texto-tenue: #55574F;
  --color-borde: #DDD8CE;
  --color-suave: #EDEAE2;          /* relleno sutil, ya no es el fondo de página */

  --color-primario: #14425C;       /* petróleo */
  --color-primario-oscuro: #0E2E40;
  --color-primario-fondo: #E5EDF1;  /* azul muy suave: hover de descargar/sustituir */

  --color-certificado-fondo: #F4E9D6;  /* ocre: documento certificado */
  --color-certificado-texto: #7A4E12;

  --color-exito-fondo: #E2EFE4;    /* verde: solo firma verificada y confirmaciones */
  --color-exito-texto: #1F5132;
  --color-error-fondo: #F6E3E1;
  --color-error-texto: #8A2020;
  --color-atencion-fondo: #FDF6E9;
  --color-atencion-texto: #6B4310;

  --radio: 10px;
  --radio-grande: 24px;
  --espacio: 1rem;
  font-size: 17px;
}
```

**Ojo con `--color-suave`.** Hoy hace de fondo de página y de relleno sutil a la vez.
Ahora son dos cosas: `body` pasa a `--color-fondo-pagina`, y `--color-suave` se queda
solo como relleno. Revisa cada uso.

## 3. Tipografía

En el `<head>` de `base.html`, antes de la hoja de estilos:

```html
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Public+Sans:ital,wght@0,400;0,500;0,600;0,700&family=Source+Serif+4:opsz,wght@8..60,600;8..60,700&display=swap" rel="stylesheet">
```

En `estilos.css`:

```css
body {
  font-family: "Public Sans", -apple-system, "Segoe UI", Roboto, sans-serif;
  background: var(--color-fondo-pagina);
}

h1, h2, .encabezado__marca {
  font-family: "Source Serif 4", Georgia, serif;
  font-weight: 700;
  letter-spacing: -0.02em;
}

h1 { font-size: 2rem; margin: 0 0 0.75rem; }
h2 { font-size: 1.25rem; }

.encabezado__marca { color: var(--color-primario); font-size: 1.3rem; }
```

Si las fuentes no cargan, el portal se ve con la tipografía del sistema y sigue siendo
usable: no hay nada que dependa de ellas.

## 4. Etiquetas de estado

Hoy son píldoras y `certificado` usa el verde. Cambian a rectángulos en versalitas, y
el certificado pasa al ocre para que deje de confundirse con la firma verificada.

```css
.etiqueta {
  display: inline-block;
  font-size: 0.72rem;
  font-weight: 700;
  letter-spacing: 0.03em;
  text-transform: uppercase;
  padding: 0.22rem 0.55rem;
  border-radius: 5px;
  white-space: nowrap;
}

.etiqueta--certificado {
  background: var(--color-certificado-fondo);
  color: var(--color-certificado-texto);
}
.etiqueta--temporal {
  background: var(--color-suave);
  color: #4A4C45;
  border: none;
}
.etiqueta--firma-valida   { background: var(--color-exito-fondo); color: var(--color-exito-texto); }
.etiqueta--firma-invalida { background: var(--color-error-fondo); color: var(--color-error-texto); }
.etiqueta--firma-ausente  { background: var(--color-suave); color: var(--color-texto-tenue); border: none; }
```

**El texto de la etiqueta nombra el origen, no una cualidad -- "Certificado" y
"Temporal" quedaron descartados.** El tipo de documento lo escribe a mano el propio
ciudadano ("certificado laboral", "certificado de estudios"), así que un mismo
documento podía leerse "Certificado" en el tipo y "TEMPORAL" en la etiqueta unas
líneas más abajo, o "CERTIFICADO" dos veces con dos significados distintos -- el
adjetivo también es un sustantivo común que la persona ya estaba usando para otra
cosa. Las etiquetas pasan a nombrar quién puso el documento ahí:

```
De una entidad     (antes "Certificado")
Subido por ti       (antes "Temporal")
```

Ninguna de las dos coincide con un tipo que alguien pueda escribir a mano, así que ya
no hace falta la línea aparte en `nota-pequena` ("Información proporcionada por ti")
debajo de un documento temporal: la etiqueta nueva dice lo mismo ella sola, sin
alarmas -- el ciudadano no está haciendo nada malo al subir su recibo escaneado.

## 5. Botones, tarjetas y campos

```css
button, .boton {
  border-radius: var(--radio);
  padding: 0.8rem 1.25rem;
  min-height: 44px;
  letter-spacing: 0.02em;
}

.tarjeta {
  border-radius: var(--radio);
  padding: 1.35rem 1.5rem;
}

input[type="text"], input[type="email"], input[type="password"],
input[type="tel"], input[type="date"], input[type="file"], select {
  border-radius: var(--radio);
  padding: 0.8rem 0.9rem;
  background: #FBFAF7;
}
```

Nada que se toque baja de 44 px de alto. `boton--peligro` se queda como está: es el
rojo, y solo aparece en trasladarse y eliminar.

**Dentro de una misma tarjeta, los pares de etiqueta y valor van en una rejilla, con
la columna de etiquetas del mismo ancho para todos**, de modo que todos los valores
arranquen en la misma vertical -- no que unos empiecen más adelante que otros porque
una etiqueta es más larga. Nada de mezclar en un mismo bloque pares en línea con
pares apilados: es una regla o la otra para todo el bloque, nunca las dos a la vez.

```css
dl.detalle {
  display: grid;
  grid-template-columns: 1fr;
  row-gap: 0.6rem;
}
dl.detalle dt { font-weight: 600; }
dl.detalle dd { margin: 0; }

@media (min-width: 800px) {
  dl.detalle {
    grid-template-columns: auto 1fr;   /* la columna de etiquetas mide lo que pida la mas larga */
    column-gap: 1.25rem;
  }
}
```

Por debajo de 800 px se apila entera (etiqueta encima, valor debajo) -- el mismo
umbral que ya usa `.entrada` para pasar de dos columnas a una (sección 8), reutilizado
aquí en vez de inventar otro. Con una columna de etiquetas fija a un ancho menor, a un
teléfono le quedan apenas unos 200 px para el valor, y un nombre largo como "Entidad
Emisora de Pruebas ColCarpeta" se parte mal en ese espacio -- 480 px no alcanzaba a
evitarlo. Sigue siendo la misma regla para todos los pares del bloque, no una mezcla.

**Datos que vienen de un sistema externo se traducen antes de mostrarse.** El sujeto
de un certificado X.509 (CU-09) es el ejemplo real: llega como una sola cadena técnica
("Common Name: Secretaría General, Organization: Entidad Emisora de Pruebas
ColCarpeta, Country: CO"), y eso no es lenguaje que un ciudadano deba leer. Se separa
en sus campos y cada uno se destaca con su propia etiqueta, en la misma rejilla
`dl.detalle` de arriba -- nunca en una aparte, y nunca varios datos metidos en un solo
`<dd>` con `<br>` (eso es justo el error que corrigió esta regla: antes "Firmante
declarado: Common Name: ..., Country: CO" salía como una sola línea de prosa dentro de
un único par etiqueta/valor):

```
Firmante    Secretaría General (certificado de prueba)
Entidad     Entidad Emisora de Pruebas ColCarpeta
País        Colombia
```

Un código (país, estado de una API) se traduce a su nombre o a una frase en español;
uno que no se reconozca se muestra tal cual, nunca se inventa un nombre para él -- ver
`app.portal.paises.nombre_pais`. El dato crudo completo (el sujeto del certificado tal
como llegó) sigue disponible para quien quiera comprobarlo, pero detrás de un
`<details>` cerrado, no como lo primero que se ve:

```html
<details>
  <summary>Ver los datos técnicos del certificado</summary>
  <p class="nota-pequena">{{ doc.firma_firmante }}</p>
</details>
```

```css
details { margin-top: 1rem; }
summary { cursor: pointer; font-weight: 600; color: var(--color-primario); padding: 0.3rem 0; }
```

Es la misma pieza que ya usa "Más filtros" en la carpeta (antes `.filtros-avanzados
summary`, generalizada a `summary` sin más para no repetirla): cualquier `<details>`
nuevo hereda el mismo resorte visual sin que la pantalla tenga que declarar nada.

**La ayuda que explica qué va a pasar después de enviar un formulario va ENCIMA del
botón, en su propio párrafo, nunca al lado.** Un botón y un párrafo puestos como
hermanos en línea dejan el texto pegado al costado del botón y se lee como si fuera
parte de él -- y si el párrafo va después del botón, en vez de antes, sigue leyéndose
como una nota al pie pegada, no como el contexto que la persona necesita antes de
decidir si envía el formulario:

```css
.ayuda--previa { margin: 0.25rem 0 1rem; }
```

```html
<p class="ayuda ayuda--previa">Si el archivo trae una firma digital, la revisamos en
segundo plano: puedes seguir usando tu carpeta mientras tanto.</p>
<button type="submit">Subir documento</button>
```

Vale para subir un documento y para sustituirlo: primero la explicación de lo que va a
pasar, con aire de sobra, después el botón.

**El texto de ayuda de un campo va debajo de ese campo, nunca al lado.** Es la misma
razón que arriba: si el campo y su explicación quedan en la misma línea, la
explicación parece una etiqueta del campo. La ayuda es siempre un bloque propio
(`.ayuda`, `display: block` sin importar dónde esté anidada -- suelta junto a un botón
o dentro de un `.campo`), nunca un `<span>` en línea.

**Un dato que no se puede cambiar se muestra como campo, con `readonly`, nunca con
`disabled`.** Así se distingue de un párrafo cualquiera y queda claro que es un dato de
la cuenta. `disabled` no vale: el campo deja de recibir foco y los lectores de pantalla
lo saltan, así que esa persona nunca se entera de cuál es su dirección de carpeta.

```css
input[readonly] {
  background: var(--color-suave);
  color: var(--color-texto-tenue);
  cursor: default;
}
```

Al lado de cada uno, una línea en `nota-pequena` que diga por qué no se puede cambiar:
la cédula identifica al ciudadano y la dirección de carpeta es su identificador
permanente (AD-10).

**Los campos obligatorios no se pintan de rojo hasta que la persona interactúe con
ellos.** La regla `input:invalid` se cumple desde que carga la página en cualquier
campo vacío, así que el formulario regaña antes de que nadie escriba nada:

```css
/* mal: regaña al cargar la página */
input:invalid { border-color: var(--color-error-texto); }

/* bien: solo después de que la persona interactuó */
input:user-invalid, .campo--error input { border-color: var(--color-error-texto); }
```

## 6. Navegación y acciones

### La navegación del encabezado no son enlaces de texto

`.encabezado__nav` no debe heredar el subrayado ni el color de los enlaces del texto
corrido. En una barra de navegación la posición ya dice que son enlaces, y subrayarlos
todos hace que compitan entre sí y que no se vea en qué página estás.

```css
.encabezado__nav a {
  text-decoration: none;
  color: var(--color-texto-tenue);
  font-weight: 500;
}
.encabezado__nav a:hover,
.encabezado__nav a:focus-visible { text-decoration: underline; }

.encabezado__nav a[aria-current="page"] {
  color: var(--color-texto);
  border-bottom: 2px solid var(--color-primario);
  padding-bottom: 3px;
}
```

La página actual se marca con `aria-current="page"`, que además se lo dice a los
lectores de pantalla. Si la plantilla no sabe en qué página está, que la ruta se lo
pase. El foco visible no se toca.

Los enlaces dentro del texto corrido siguen subrayados: ahí sí hacen falta.

**Cerrar sesión tiene que verse igual que los demás elementos de la barra**, aunque por
dentro sea distinto: es un `<button>` dentro de un formulario POST, porque cierra la
sesión y eso no puede ser un enlace. Que sea un botón no obliga a que lo parezca. Lo que
se iguala es el aspecto, no el elemento:

```css
.encabezado__nav button {
  background: none;
  border: none;
  padding: 0;
  font: inherit;
  font-weight: 500;
  color: var(--color-texto-tenue);
  cursor: pointer;
}
.encabezado__nav button:hover,
.encabezado__nav button:focus-visible { text-decoration: underline; }
```

**Regla general: un `<button>` que se presenta como otra cosa tiene que anular TODOS
sus estados, no solo el de reposo.** Ya mordió dos veces con el mismo botón de "Cerrar
sesión": igualar el aspecto en reposo (fondo y borde en `none`) no alcanza, porque la
regla genérica `button:hover` sigue aplicando por su cuenta y tiene la misma
especificidad que un selector de una sola clase -- gana por aparecer después en la hoja
de estilos, y el botón se rellena de sólido al pasar el cursor. La forma correcta es
neutralizar también `:hover`, `:focus-visible` y `:active`, con un selector que
combine la clase del contexto (o del propio botón) con el estado, para que su
especificidad supere siempre a la regla genérica:

```css
.encabezado__nav button:hover,
.encabezado__nav button:focus-visible,
.encabezado__nav button:active {
  background: none;
  border-color: transparent;
  box-shadow: none;
}
```

Aplica a cualquier `<button>` disfrazado de enlace o de texto plano -- no solo a
"Cerrar sesión": `.boton-enlace` (ver más abajo) tiene el mismo problema y la misma
solución.

### Enlace o botón según lo que haga

- **Navega** (descargar, sustituir, ir a otra pantalla) → `<a href>`.
- **Cambia algo** (eliminar, marcar como leída, solicitar) → `<button>` dentro de un
  formulario que haga POST. **Nunca un enlace.** Un enlace que borra se puede disparar
  solo con que el navegador precargue enlaces o con que pase un rastreador.

Que sea un botón no obliga a que parezca un botón: `boton-enlace` existe para eso.

**Regla general: varias acciones dentro de un mismo grupo comparten geometría y se
diferencian solo por color y peso.** Un grupo donde una acción es una caja con borde
y otra es texto suelto está mal, porque obliga a leer para entender que las dos se
pueden pulsar -- ya pasó en `/documentos/{id}`, donde Descargar era una caja
(`boton--secundario`) y Sustituir/Eliminar eran texto subrayado. Dentro de un mismo
grupo, mismo alto, mismo relleno, mismo radio, mismo borde (heredados de `.boton`);
lo único que cambia es el color, según lo que hace cada acción:

```css
.boton--neutro {
  background: transparent;
  color: var(--color-texto-tenue);
  border-color: var(--color-borde);
}
.boton--peligro-discreto {
  background: transparent;
  color: var(--color-error-texto);
  border-color: var(--color-error-texto);
}
```

- **Descargar** (la acción habitual) → `boton--secundario` (borde y texto en el color
  primario).
- **Sustituir** (secundaria, ocasional) → `boton--neutro` (borde y texto neutros).
- **Eliminar**, cuando convive con otras acciones del mismo peso → `boton--peligro-
  discreto` (borde y texto en rojo, pero sin relleno sólido).

`boton--peligro` (relleno sólido) se queda para cuando eliminar -- o trasladarse, o
deshabilitar el segundo factor -- es la única acción grave de la pantalla, sin nada al
lado con lo que tenga que emparejarse. La diferencia de peso entre un grupo y una
acción sola sigue siendo válida; lo que no vale es mezclar pesos *dentro* del mismo
grupo.

En bloques más compactos (la fila de un documento en la lista de la carpeta) se probó
`boton-enlace` para una geometría más chica que la de `.boton` completo -- y no
funcionó: `boton-enlace` no fija su propio `min-height`, así que un `<a
class="boton-enlace">` (Descargar, Sustituir) queda con la altura que le den su
padding y su texto, mientras que un `<button class="boton-enlace ...">` (Eliminar,
que tiene que ser un botón porque hace POST) sigue emparejando con el selector
genérico `button, .boton { min-height: 44px; ... }` de arriba -- la regla de la etiqueta
(clase) no pisa esa altura mínima, solo el resto de las propiedades. El resultado: las
tres acciones de la misma fila con alturas distintas, exactamente el problema que esta
regla existe para evitar. La fila de la carpeta usa hoy la misma solución que el
detalle de un documento: `.boton` completo con las mismas tres variantes de color
(`boton--secundario`, `boton--neutro`, `boton--peligro-discreto`) -- `boton-enlace`
sigue existiendo para otros usos (el enlace "Más filtros", por ejemplo, no es un grupo
de acciones), pero dejó de recomendarse para agrupar acciones de distinto tipo de
elemento (`<a>` y `<button>` mezclados). Todo lo que sí siga usando `boton-enlace`
tiene que avisar de que es pulsable al pasar el cursor, no solo llevar subrayado --
`boton-enlace--peligro` ya tiene su propio hover en rojo; el resto recibe el mismo
tratamiento pero en azul, también en `:focus-visible`, para que quien navega con
teclado vea la misma señal que quien usa el ratón:

```css
.boton-enlace {
  border: 1px solid transparent;  /* reservado, para que el hover no mueva nada */
  padding: 0.3rem 0.5rem;
  border-radius: 6px;
}
.boton-enlace:hover,
.boton-enlace:focus-visible,
.boton-enlace:active {
  background: var(--color-primario-fondo);
  border-color: var(--color-primario);
}
```

### Confirmar antes de una acción irreversible: un `<dialog>` propio, no `window.confirm()`

`window.confirm()` muestra el nombre del dominio ("127.0.0.1:8000 dice") y no se puede
estilar. `<dialog>` es HTML nativo: ya trae el foco atrapado dentro de él y el cierre
con `Escape`, sin ninguna librería. Un solo diálogo, compartido por todas las
confirmaciones (eliminar un documento, cerrar sesión):

```html
<dialog id="dialogo-confirmar" class="dialogo">
  <form method="dialog">
    <p id="dialogo-confirmar-texto"></p>
    <div class="acciones">
      <button type="submit" value="cancelar" class="boton boton--neutro" autofocus>Cancelar</button>
      <button type="submit" value="confirmar" class="boton boton--peligro">Confirmar</button>
    </div>
  </form>
</dialog>
```

```css
.dialogo { border: none; border-radius: var(--radio); padding: 1.5rem; max-width: 26rem; width: calc(100% - 2rem); }
.dialogo::backdrop { background: rgba(27, 29, 26, 0.5); }
.dialogo .acciones { margin-top: 1.25rem; justify-content: flex-end; }
```

- **Cancelar va en neutro y con el foco inicial** (`autofocus`), para que pulsar
  `Enter` sin mirar no confirme nada por accidente.
- **Confirmar va en rojo** (`boton--peligro`, sólido: es la única acción de un diálogo
  pequeño y enfocado, no un grupo de acciones que deba ir discreto).
- **La acción nunca depende del diálogo.** Cada formulario se sigue enviando de forma
  normal si el JavaScript no carga o falla -- el diálogo es una capa que se agrega
  encima con `event.preventDefault()` en el `submit`, nunca el único camino hacia la
  petición real. Un formulario corriente marca su mensaje con `data-confirmar="..."`;
  uno con HTMX ya dispara `htmx:confirm` con el texto de su `hx-confirm`, así que
  basta con escuchar ese evento una sola vez y reemplazar su `window.confirm()` por
  este mismo diálogo -- sin tocar el atributo `hx-confirm` que ya tuviera.

Se usa para eliminar un documento (desde la lista y desde el detalle) y para cerrar
sesión -- cualquier acción donde un clic sin querer sea costoso de deshacer.

### El desplegable de operadores solo muestra destinos utilizables

El directorio del MinTIC es dato sucio (CLAUDE.md, "trampa 5"): trae URLs sin TLS,
duplicadas, o que ni siquiera apuntan a un host real (`http://0.0.0.0:8000`, dominios
sueltos sin punto). El desplegable de operador destino en la pantalla de traslado no
puede mostrar una entrada así -- solo confunde, porque el envío la rechazaría de
todas formas.

**El filtro que decide qué operador sirve vive en una sola función, y la usan por
igual el desplegable, la ruta que recibe la solicitud y el envío real en segundo
plano.** Nunca una copia del mismo chequeo en tres lugares, porque en cuanto uno de
los tres se desactualiza dejan de estar de acuerdo. Descarta: URL ausente o mal
formada, esquema distinto de `https` (o de `http`/`https` si
`TRANSFERENCIA_EXIGIR_HTTPS` está apagado).

**El chequeo de host "público" (nada de `localhost`, `0.0.0.0`, `127.x.x.x`, ni un
host sin punto) solo aplica cuando `TRANSFERENCIA_EXIGIR_HTTPS` está prendido.** Esa
misma bandera ya distingue "estamos contra el directorio real" de "instancias propias
de prueba sin TLS" (`docker-compose.test.yml`) -- y ahí los nombres de servicio de
Docker (`app-b`, sin punto) son exactamente lo esperado, no dato sucio. Aplicar el
chequeo de host siempre habría roto el entorno de pruebas local documentado en
CLAUDE.md.

Si tras filtrar no queda ningún operador, la pantalla lo dice con claridad en vez de
mostrar un desplegable vacío sin explicación.

## 7. La carpeta: dos páginas

### Estructura

Subir un documento vive en su propia página, no encima de la lista.

- **`/carpeta`** — solo la lista y sus filtros. Arriba, un botón claro que lleva a
  subir.
- **`/carpeta/subir`** — el formulario con sus campos y su ayuda, con espacio, y un
  enlace para volver.
- Al subir con éxito se vuelve a `/carpeta` con el aviso de confirmación y el documento
  ya en la lista.

La carpeta abre mostrando lo que el ciudadano tiene, no un formulario. Si el formulario
va primero, la pantalla deja de ser un archivador y pasa a ser un trámite.

Los filtros no pueden pesar más que los documentos que filtran: visible la búsqueda por
título y el tipo; el resto dentro de un `<details>` cerrado, que se abre solo si alguno
trae valor. **Al desplegar "Más filtros", los botones de filtrar y limpiar no se
descolocan**: viven fuera del `<details>`, en su propia fila al final del formulario, y
se quedan donde están abra o cierre.

### Ancho

El contenedor de 720 px deja demasiado vacío a los lados en un monitor. Sube a
**1120 px**, y que la barra del encabezado use ese mismo ancho para que su contenido
quede alineado con el de la página.

```css
.contenedor { max-width: 1120px; }
```

Dos cosas que ese ancho no debe arrastrar:

- **El texto corrido no se estira hasta 1120 px**, porque una línea tan larga se lee
  mal. Los párrafos de ayuda, avisos y explicaciones se quedan en unos 68 caracteres
  (`max-width: 62ch`).
- **Los formularios de una sola columna tampoco.** Un campo de 1120 px de ancho para
  escribir un teléfono se ve roto. Los formularios se quedan en unos 560 px, o pasan a
  dos columnas donde tenga sentido.

### La columna derecha

A partir de 1000 px, `/carpeta` se parte en dos: la lista a la izquierda y una columna
de unos 320 px a la derecha con el **espacio usado** —su propia tarjeta, con la barra y
las cifras— y una tarjeta con el **operador actual** y el enlace de traslado. Por debajo
de ese ancho se apila: primero la lista, después esas tarjetas.

La cuota merece su tarjeta y no una línea suelta bajo el título: es un dato que el
ciudadano consulta, no un adorno del encabezado. Dentro van la barra, el consumo frente
al límite y la línea que aclara que los documentos certificados por una entidad no
ocupan espacio.

## 8. Entrada: tarjeta partida

Las pantallas de iniciar sesión (`sesion.html`) y registro (`registro.html`) pasan a una
tarjeta partida en dos mitades, con una curva grande donde se encuentran. En una mitad
va el formulario; en la otra, un panel de color que invita a la acción contraria.

- En **iniciar sesión**: el panel va a la derecha e invita a crear la carpeta.
- En **registro**: el panel va a la izquierda e invita a entrar.

```css
.entrada {
  display: flex;
  background: var(--color-fondo);
  border: 1px solid #E5E1D7;
  border-radius: var(--radio-grande);
  overflow: hidden;
  min-height: 560px;
}

.entrada__forma {
  flex: 1 1 55%;
  padding: 3rem 3.5rem;
  display: flex;
  flex-direction: column;
  justify-content: center;
}

.entrada__panel {
  flex: 1 1 45%;
  background: var(--color-primario);
  color: #FFFFFF;
  padding: 3rem;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  text-align: center;
  border-radius: 180px 0 0 180px;
}
.entrada__panel h2 { color: #FFFFFF; margin: 0 0 0.75rem; }
.entrada__panel p  { color: #D4E0E8; max-width: 22rem; margin: 0 0 2rem; line-height: 1.6; }
.entrada__panel .boton {
  background: transparent;
  border: 1.5px solid #FFFFFF;
  color: #FFFFFF;
}
.entrada__panel .boton:hover { background: rgba(255,255,255,0.12); }

/* registro: el panel a la izquierda */
.entrada--invertida .entrada__panel { order: -1; border-radius: 0 180px 180px 0; }

@media (max-width: 800px) {
  .entrada { flex-direction: column; min-height: 0; }
  .entrada__forma { padding: 2rem 1.5rem; }
  .entrada__panel,
  .entrada--invertida .entrada__panel {
    order: 0;
    border-radius: 0;
    padding: 2.25rem 1.5rem;
  }
}
```

En el teléfono la tarjeta se apila: primero el formulario, después el panel. La curva
desaparece porque a 390 px de ancho no cabe.

### Las dos tarjetas miden lo mismo

Iniciar sesión y registro comparten la clase `.entrada`, así que ya comparten ancho y
`min-height`. Lo que las desigualaba era el contenido: el formulario de registro tenía
seis campos en una sola columna y crecía muy por encima del mínimo compartido, así que
saltar de una pantalla a la otra se sentía brusco por el cambio de alto de página, no
solo por el color del panel.

**Los campos que van juntos por tema van en dos columnas** (cédula y nombre, dirección
y teléfono), para que el formulario deje de ser una lista larga:

```css
.campos-doble { display: grid; grid-template-columns: 1fr; gap: 0 1rem; }
@media (min-width: 480px) {
  .campos-doble { grid-template-columns: 1fr 1fr; }
}
```

Por debajo de 480 px se queda en una columna: a ese ancho, cédula y teléfono lado a
lado quedan demasiado angostos para escribir con comodidad. Correo personal y
contraseña siguen en una columna aparte, siempre: son los campos más largos de leer y
de escribir.

**Opcional, sin JavaScript:** para suavizar el salto entre las dos pantallas en los
navegadores que lo soportan,

```css
@view-transition {
  navigation: auto;
}
```

En los navegadores sin soporte no pasa nada -- ni un error, ni un salto peor que el de
hoy.

### Qué NO va en esa pantalla

- **Ningún botón de Google, Facebook, GitHub o LinkedIn.** ColCarpeta no tiene inicio
  de sesión con esos servicios. Pintarlos sería inventar una función que no existe.
- **Ningún "¿Olvidaste tu contraseña?".** Tampoco está implementado. El enlace que sí
  existe es el de primer acceso, para quien llegó por un traslado.

### Textos del panel

Iniciar sesión:

> **¿Primera vez aquí?**
> Crea tu carpeta ciudadana y reúne en un solo lugar los documentos que hoy tienes
> repartidos entre entidades.
> [ CREAR MI CARPETA ]

Registro:

> **¿Ya tienes carpeta?**
> Entra con tu cédula o con la dirección de correo que generamos para ti.
> [ ENTRAR ]

## 9. Marca

Los archivos ya están en `app/portal/static/`. No hace falta generarlos ni buscarlos
en otro lado.

| Archivo | Para qué |
| --- | --- |
| `icono.svg` | El icono. Sirve a cualquier tamaño; es el que va en el encabezado. |
| `favicon.ico` | Pestaña del navegador. Lleva 16, 32 y 48 px dentro. |
| `icono-180.png` | Pantalla de inicio en iOS. |
| `icono-192.png`, `icono-512.png` | Android y el manifiesto. |
| `site.webmanifest` | Nombre, colores e iconos de la aplicación instalada. |
| `marca.svg` | El icono con el nombre al lado. **Solo para fuera del navegador.** |

El icono es una carpeta con la C de ColCarpeta: lengüeta en ocre
(`--color-certificado-texto`), cuerpo en petróleo (`--color-primario`), la C calada en
papel. Usa la misma paleta que el portal a propósito.

En el `<head>` de `base.html`:

```html
<link rel="icon" href="/static/favicon.ico" sizes="32x32">
<link rel="icon" href="/static/icono.svg" type="image/svg+xml">
<link rel="apple-touch-icon" href="/static/icono-180.png">
<link rel="manifest" href="/static/site.webmanifest">
<meta name="theme-color" content="#14425C">
```

En el encabezado, `encabezado__marca` lleva el icono a la izquierda del nombre:

```html
<a class="encabezado__marca" href="/">
  <img src="/static/icono.svg" alt="" width="28" height="28">
  ColCarpeta
</a>
```

```css
.encabezado__marca { display: inline-flex; align-items: center; gap: 0.55rem; }
```

El `alt` va vacío a propósito: el nombre está escrito al lado y repetirlo sería ruido
para un lector de pantalla.

**No uses `marca.svg` dentro del portal.** Ahí el icono y el texto por separado quedan
mejor y respetan la tipografía cargada. En `marca.svg` el nombre va como texto, así que
fuera del navegador se verá con una tipografía de reemplazo; sirve para documentos y
presentaciones, no para la aplicación.

No redibujes el icono ni generes variantes nuevas. Si hiciera falta otro tamaño, sale
de `icono.svg`.

## 10. Lo que no se toca

- La estructura de las plantillas y los nombres de las clases existentes. Solo se
  agregan las de la tarjeta partida.
- El enlace de saltar al contenido, el foco visible, los `autocomplete`, los `label`
  asociados a cada campo y el `lang="es"`. Todo eso ya está bien.
- Cualquier ruta, respuesta o contrato de la API.
