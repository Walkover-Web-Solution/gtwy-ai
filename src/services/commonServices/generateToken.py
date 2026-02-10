import jwt


def generateToken(payload, accessKey):
    try:
        return jwt.encode(payload, accessKey, algorithm="HS256")
    except Exception as error:
        print("Error generating token:", error)
        raise Exception("Failed to generate token") from error


# Assuming you want to export it in a similar way to how ES6 modules work
generateTokenModule = {"generateToken": generateToken}
