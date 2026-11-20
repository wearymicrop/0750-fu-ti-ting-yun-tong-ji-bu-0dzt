export async function request(path, body) {
  const options = body === undefined ? {} : {
    method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body),
  };
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) {
    const error = new Error(data.message ?? '请求失败，请重试');
    error.code = data.code;
    error.details = data.details;
    throw error;
  }
  return data;
}
