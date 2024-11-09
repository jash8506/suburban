#include <ArduinoRS485.h>  // ArduinoModbus depends on the ArduinoRS485 library
#include <ArduinoModbus.h>

constexpr auto baudrate{ 9600 };

// Calculate preDelay and postDelay in microseconds as per Modbus RTU Specification
// MODBUS over serial line specification and implementation guide V1.02
// Paragraph 2.5.1.1 MODBUS Message RTU Framing
// https://modbus.org/docs/Modbus_over_serial_line_V1_02.pdf
constexpr auto bitduration{ 1.f / baudrate };
constexpr auto preDelayBR{ bitduration * 9.6f * 3.5f * 1e6 };
constexpr auto postDelayBR{ bitduration * 9.6f * 3.5f * 1e6 };

#include <Arduino.h>
#include <Ethernet.h>
#include <ArduinoHttpClient.h>
#include <opta_info.h>
#include <ArduinoJson.h>

#define HOME_PATH "/"
#define POWER_METER_PATH "/power"
#define HTTP_GET "GET"
#define HTTP_POST "POST"
#define MAX_PATH_LEN 2048
#define MAX_METHOD_LEN 16

OptaBoardInfo *info;
OptaBoardInfo *boardInfo();

int port = 80;
// Ethernet server on port 80.
EthernetServer server(port);

// Reserve 256 bytes for the JSON.
StaticJsonDocument<256> response;

// Carlo Gavazzi EM112 series single phase Energy Monitor
// values are actually not these units, but factored: V*10, A*1000, W*10,VA*10,VAR*10,W*10,W*10
// These are 16 bit values read below. PF*1000,Hz*10
#define REG_COUNT 7
String registers[REG_COUNT] = {
  "V",
  "A",
  "W",
  "VA",
  "VAR",
  "W dmd",
  "W dmd peak",
};
bool is_signed[REG_COUNT] = { false, true, true, false, false, false, false };
#define DEBUG_MODE true

void setup() {
  Serial.begin(9600);
  while (!Serial)
    ;
  // Modbus setup
  Serial.println("Modbus RTU Client");
  RS485.setDelays(preDelayBR, postDelayBR);
  // Start the Modbus RTU client
  if (!ModbusRTUClient.begin(baudrate)) {
    Serial.println("Failed to start Modbus RTU Client!");
    while (1)
      ;
  }

  // start the Ethernet connection:
  if (!Ethernet.begin()) {
    Serial.println("Failed to initialize Ethernet library");
    return;
  }

  // Start to listen
  server.begin();
  Serial.println(("Server is ready."));
  Serial.print("Please connect to http://");
  Serial.println(Ethernet.localIP());
}

void loop() {
  delay(500);
  Serial.print('regcount: ');
  Serial.println(REG_COUNT * 2);
  // Modbus poll. Reg count is 32 bit registers, then 2 more for PF/Hz
  if (!ModbusRTUClient.requestFrom(1, INPUT_REGISTERS, 0x00, REG_COUNT * 2 + 2)) {
    Serial.print("failed to read registers! ");
    Serial.println(ModbusRTUClient.lastError());
  } else {
    for (int ix = 0; ix < REG_COUNT; ix++) {
      // power monitor values are all 32 bit
      uint16_t bits[2];
      bits[1] = ModbusRTUClient.read();
      bits[0] = ModbusRTUClient.read();
      uint32_t full;
      int32_t full_signed;
      if (is_signed[ix]) {
        memcpy(&full_signed, bits, 4);
      } else {
        memcpy(&full, bits, 4);
      }
      // set the json value
      response[registers[ix]] = full;
      if (DEBUG_MODE) {
        Serial.print(registers[ix]);
        Serial.print(" : ");
        Serial.println(full);
        Serial.print(bits[0]);
        Serial.print(" , ");
        Serial.println(bits[1]);
      }
    }
    // set the json value
    response['PF'] = ModbusRTUClient.read();
    response['Hz'] = ModbusRTUClient.read();
  }

  // Wait for an incomming connection
  EthernetClient client = server.available();

  if (client) {
    HttpClient http = HttpClient(client, Ethernet.localIP(), port);
    IPAddress clientIP = client.remoteIP();
    Serial.println("Client with address " + clientIP.toString() + " available.");

    while (client.connected()) {
      if (client.available()) {
        // Read HTTP method and path from the HTTP call.
        char method[MAX_METHOD_LEN], path[MAX_PATH_LEN];
        getHttpMethodAndPath(&http, method, path);

        // If path matches "/".
        if (strncmp(path, HOME_PATH, MAX_PATH_LEN) == 0) {
          Serial.println("Client with address " + clientIP.toString() + " connected to '/'...");
          // This endpoint only accepts GET requests.
          if (strncmp(method, HTTP_GET, MAX_METHOD_LEN) == 0) {
            // sendHomepage(&client);
          } else if (strncmp(method, HTTP_POST, MAX_METHOD_LEN) == 0) {
            // Skip headers and read POST request body.
            // http.skipResponseHeaders();
            // String body = http.readString();
            // // In case of POST requests with a body change LEDs states.
            // if (body != "") {
            //   parseRequest(body);
            // }
            // In case of POST requests also respond with LEDs states.
            sendPower(&client);
          } else {
            badRequest(&client);
          }
        }
        // If path matches "/power".
        else if (strncmp(path, POWER_METER_PATH, MAX_PATH_LEN) == 0) {
          Serial.println("Client with address " + clientIP.toString() + " connected to '/power'...");
          // This endpoint accepts both GET and POST requests.
          if (strncmp(method, HTTP_GET, MAX_METHOD_LEN) == 0) {
            // Respond to GET with LEDs states.
            sendPower(&client);
          }

          else {
            badRequest(&client);
          }
        } else {
          Serial.println("Client with address " + clientIP.toString() + " attempted connection to " + String(path));
          notFound(&client);
        }
        // Consume RX buffer to prepare for next request.
        consumeRxBuffer(&http);
      }
    }

    Serial.println("Client with address " + clientIP.toString() + " disconnected.");
  }
}


// StaticJsonDocument<128> req;

// void parseRequest(String body) {
//   // Deserialize request body.
//   char bodyChar[body.length() + 1];
//   body.toCharArray(bodyChar, sizeof(bodyChar));
//   DeserializationError error = deserializeJson(req, bodyChar);

//   // Test if parsing succeeds.
//   if (error) {
//     Serial.print("JSON deserialization error: ");
//     Serial.println(error.f_str());
//     return;
//   } else {
//     // Print the request and change LEDs states accordingly.
//     Serial.print("Request: ");
//     for (int i = 0; i <= 3; i++) {
//       String led = "LED_D" + String(i);
//       String value = req[led];
//       controlLED(i, value);
//       Serial.print(led + " to " + value + ", ");
//     }
//     Serial.println();
//   }
// }

// void controlLED(int led, String value) {
//   if (getState(value) != -1) {
//     [led] = value;
//   }
//   switch (led) {
//     case 0:
//       digitalWrite(LED_D0, getState(ledStates[led]));
//       break;
//     case 1:
//       digitalWrite(LED_D1, getState(ledStates[led]));
//       break;
//     case 2:
//       digitalWrite(LED_D2, getState(ledStates[led]));
//       break;
//     case 3:
//       digitalWrite(LED_D3, getState(ledStates[led]));
//       break;
//   }
// }

// int getState(String state) {
//   if (state == "LOW") {
//     return LOW;
//   } else if (state == "HIGH") {
//     return HIGH;
//   } else {
//     return -1;
//   }
// }

void getHttpMethodAndPath(HttpClient *http, char *method, char *path) {
  size_t l = http->readBytesUntil(' ', method, MAX_METHOD_LEN - 1);
  method[l] = '\0';

  l = http->readBytesUntil(' ', path, MAX_PATH_LEN - 1);
  path[l] = '\0';
}

void sendPower(EthernetClient *client) {
  Serial.print("Sending: ");
  serializeJson(response, Serial);
  Serial.println();
  // Sent HTTP headers.
  client->println("HTTP/1.1 200 OK");
  client->println("Connection: close");
  client->println("Content-Type: application/json");

  // Compute JSON body Content Length and finisha headers.
  String size = String(measureJsonPretty(response));
  client->print("Content-Length: ");
  client->println(measureJsonPretty(response));
  client->println();

  // Send serialized JSON body.
  serializeJsonPretty(response, *client);
  client->stop();
}

void notFound(EthernetClient *client) {
  client->println("HTTP/1.1 404 Not Found");
  client->println("Connection: close");
  client->println("Content-Length: 0");
  client->println();
  Serial.println("Not Found [404]");
}

void badRequest(EthernetClient *client) {
  client->println("HTTP/1.1 400 Bad Request");
  client->println("Connection: close");
  client->println("Content-Length: 0");
  client->println();
  Serial.println("Bad Request [400]");
}

void consumeRxBuffer(HttpClient *http) {
  // Consume headers in RX buffer.
  http->skipResponseHeaders();
  // Consume body in RX buffer if it exists.
  if (http->contentLength() > 0) {
    http->responseBody();
  }
}
