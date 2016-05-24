import argparse
import asyncio

import aiocoap

def b2a(rawdata):
    # like binascii.b2a_hex, but directly to unicode for printing, and with nice spacing
    return " ".join("%02x"%b for b in rawdata)

class ParseError(ValueError): """Raised by ESP2Message and similar's .parse() method when data is unexpected"""
class TimeoutError(Exception): """Raised by exchange if the bus timeout is encountered, or a FAM responded with a timeout message."""

async def exchange(context, address, request, responsetype):
    # FIXME this should do more error handling
    coap_request = aiocoap.Message(code=aiocoap.POST, uri=address, payload=request.serialize())
    coap_response = await context.request(coap_request).response

    try:
        return responsetype.parse(coap_response.payload)
    except ParseError:
        try:
            EltakoTimeout.parse(coap_response.payload)
        except ParseError:
            pass
        else:
            raise TimeoutError
        raise

class ESP2Message:
    def __init__(self, body):
        self.body = body

    def serialize(self):
        return b"\xa5\x5a" + self.body + bytes([sum(self.body) % 256])

    @classmethod
    def parse(cls, data):
        if data[:2] != b"\xa5\x5a":
            raise ParseError("No preamble found")
        if len(data) != 14:
            raise ParseError("Invalid message length")

        body = data[2:13]
        if sum(body) % 256 != data[13]:
            raise ParseError("Checksum mismatch")

        return ESP2Message(body)

    def __repr__(self):
        return "<%s %r>"%(type(self).__name__, self.body)

class EltakoMessage(ESP2Message):
    def __init__(self, org, address, payload=b"\0\0\0\0\0\0\0\0", is_request=True):
        self.org = org
        self.address = address
        self.payload = payload
        self.is_request = is_request

    body = property(lambda self: bytes((((5 if self.is_request else 4) << 5) + 11, self.org, *self.payload, self.address)))

    @classmethod
    def parse(cls, data):
        esp2message = super().parse(data)
        try:
            is_request = {(5 << 5) + 11: True, (4 << 5) + 11: False}[esp2message.body[0]]
        except KeyError:
            raise ParseError("Code is neither TCT nor RMT")
        org = esp2message.body[1]
        address = esp2message.body[10]
        payload = esp2message.body[2:10]
        return EltakoMessage(org, address, payload, is_request)

    def __repr__(self):
        return "<%s %s ORG %02x ADDR %02x, %s>"%(type(self).__name__, ["Response", "Request"][self.is_request], self.org, self.address, b2a(self.payload))

class EltakoDiscoveryReply(EltakoMessage):
    org = 0xf0
    is_request = False
    address = 0

    def __init__(self, reported_address, reported_size, model):
        self.reported_address = reported_address
        self.reported_size = reported_size
        self.model = model

    payload = property(lambda self: bytes((self.reported_address, self.reported_size, 0x7f, 0x08)) + self.model)

    @classmethod
    def parse(cls, data):
        eltakomessage = super().parse(data)
        if eltakomessage.org != cls.org or eltakomessage.is_request != cls.is_request or eltakomessage.address != cls.address:
            raise ParseError("This is not an EltakoDiscoveryReply")
        if eltakomessage.payload[2:4] != b"\x7f\x08":
            raise ParseError("Assumed fixed part 7F 08 not present")
        reported_address = eltakomessage.payload[0]
        reported_size = eltakomessage.payload[1]
        model = eltakomessage.payload[4:8]
        return EltakoDiscoveryReply(reported_address, reported_size, model)

    def __repr__(self):
        return "<%s address %d size %d, model %s>"%(type(self).__name__, self.reported_address, self.reported_size, b2a(self.model))

class EltakoTimeout(EltakoMessage):
    org = 0xf8
    is_request = False
    payload = b"\0\0\0\0\0\0\0\0"
    address = 0

    def __init__(self):
        pass

    @classmethod
    def parse(cls, data):
        esp2message = super().parse(data)
        if esp2message.org != cls.org or esp2message.is_request != cls.is_request or esp2message.payload != cls.payload or esp2message.address != cls.address:
            raise ParseError("Does not look like an EltakoTimeout")
        return EltakoTimeout()

async def enumerate(context, address):
    print("Scanning the bus for devices with addresses...")
    usage_map = [None]
    while len(usage_map) < 255:
        try:
            response = await exchange(context, address, EltakoMessage(org=0xf0, address=len(usage_map)), EltakoDiscoveryReply)
        except TimeoutError:
            usage_map.append(False)
        else:
            assert len(usage_map) == response.reported_address
            print("Discovered at %d: Device sized %d, type %s"%(len(usage_map), response.reported_size, b2a(response.model)))
            for i in range(response.reported_size):
                usage_map.append(True)

    print("Bus scan completed.")

    reported = False
    while True:
        try:
            response = await exchange(context, address, EltakoMessage(org=0xf0, address=0), EltakoDiscoveryReply)
        except TimeoutError:
            if reported is False:
                print("You may now put a device into LRN mode to automatically assign an address.")
                reported = True
        else:
            print("A device is available in LRN mode (model %s, size %d)."%(b2a(response.model), response.reported_size))
            for i in range(1, 254 - response.reported_size):
                if not any(usage_map[i:i+response.reported_size]):
                    break
            else:
                raise Exception("No suitable free space in usage map")

            usage_map[i:i+response.reported_size] = [True] * response.reported_size
            response = await exchange(context, address, EltakoMessage(org=0xf8, address=i), EltakoDiscoveryReply)
            if response.reported_address == 0:
                print("Assigning may not have worked, marking area as dirty and trying again...")
                continue
            assert response.reported_address == i, "Assigning bus number %d resulted in response %r"%(i, response)
            print("The device was assigned bus address %d. You may now put a further device into LRN mode."%i)

async def assign(context, address, new_busaddress):
    eltako_request = EltakoMessage(org=0xf0, address=0)

    request = aiocoap.Message(code=aiocoap.POST, uri=address, payload=eltako_request.serialize())
    response = await context.request(request).response

    eltako_response = EltakoMessage.parse(response.payload)
    print(eltako_response)

    eltako_request = EltakoMessage(org=0xf8, address=new_busaddress)

    request = aiocoap.Message(code=aiocoap.POST, uri=address, payload=eltako_request.serialize())
    response = await context.request(request).response

    eltako_response = EltakoMessage.parse(response.payload)
    print(eltako_response)

def main():
    p = argparse.ArgumentParser()
    p.add_argument("rawuri", help="URI at which a raw ESP2 resource is exposed")
    opts = p.parse_args()

    loop = asyncio.get_event_loop()
    context = loop.run_until_complete(aiocoap.Context.create_client_context())
    loop.run_until_complete(enumerate(context, opts.rawuri))

if __name__ == "__main__":
    main()
