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
bool is_signed[REG_COUNT] = { false, true, true, false, true, false, false };
#define DEBUG_MODE false

void setup() {
  if (DEBUG_MODE) {
    Serial.begin(9600);
    // while (!Serial)
    //   ;
    Serial.println("Modbus RTU Client");
  }
  // Modbus setup
  RS485.setDelays(preDelayBR, postDelayBR);
  // Start the Modbus RTU client
  if (!ModbusRTUClient.begin(baudrate)) {
    if (DEBUG_MODE) {
      Serial.println("Failed to start Modbus RTU Client!");
    }
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
  if (DEBUG_MODE) {
    Serial.println(("Server is ready."));
    Serial.print("Please connect to http://");
    Serial.println(Ethernet.localIP());
  }
  Serial.end();
}

void loop() {
  Ethernet.maintain();
  if (!server) {
    server.begin();
  }
  // Modbus poll. REG_COUNT 32 bit registers, then 2 (16 bit) more for PF/Hz
  int registers_to_read = (REG_COUNT * 2) + 2;

  if (!ModbusRTUClient.requestFrom(1, INPUT_REGISTERS, 0x00, registers_to_read)) {
    // Serial.print("failed to read registers! ");
    // Serial.println(ModbusRTUClient.lastError());
  } else {
    for (int ix = 0; ix < REG_COUNT; ix++) {
      // power monitor values are all 32 bit
      uint16_t bits[2];
      bits[0] = ModbusRTUClient.read();
      bits[1] = ModbusRTUClient.read();
      uint32_t full;
      int32_t full_signed;
      if (is_signed[ix]) {
        memcpy(&full_signed, bits, 4);
        response[registers[ix]] = full_signed;
      } else {
        memcpy(&full, bits, 4);
        response[registers[ix]] = full;
      }
      // set the json value
      if (DEBUG_MODE) {
        Serial.print(registers[ix]);
        Serial.print(" : ");
        if (is_signed[ix]) {
          Serial.println(full_signed);
        } else {
          Serial.println(full);
        }
        Serial.print(bits[0]);
        Serial.print(" , ");
        Serial.println(bits[1]);
      }
    }
    // set the json value
    uint16_t pf = ModbusRTUClient.read();
    response["PF"] = pf;
    uint16_t hz = ModbusRTUClient.read();
    response["Hz"] = hz;
  }

  // Wait for an incoming connection
  EthernetClient client = server.available();

  if (client) {
    HttpClient http = HttpClient(client, Ethernet.localIP(), port);
    IPAddress clientIP = client.remoteIP();
    if (DEBUG_MODE) {
      Serial.println("Client with address " + clientIP.toString() + " available.");
    }

    // Read the request (we ignore the request for now)
    while (client.available())
      client.read();
    sendPower(&client);
    return;
    client.stop();
  } else {
    // Serial.println('faaail');
  }
}

void sendPower(EthernetClient *client) {
  if (DEBUG_MODE) {
    Serial.print("Sending: ");
    serializeJson(response, Serial);
    Serial.println();
  }
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
}
